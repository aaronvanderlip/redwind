from . import push
from . import app
from . import queue
from . import archiver
from .models import Post, Metadata, acquire_lock

from flask import request, make_response, render_template, url_for, jsonify
from werkzeug.exceptions import NotFound

import urllib.parse
import urllib.request
import requests
import json
import os

from bs4 import BeautifulSoup


@app.route('/webmention', methods=['GET', 'POST'])
def receive_webmention():
    if request.method == 'GET':
        return render_template('webmention.html')

    source = request.form.get('source')
    target = request.form.get('target')
    callback = request.form.get('callback')

    if not source:
        return make_response(
            'webmention missing required source parameter', 400)

    if not target:
        return make_response(
            'webmention missing required target parameter', 400)

    app.logger.debug("Webmention from %s to %s received", source, target)
    delayed = process_webmention.delay(source, target, callback)

    key = delayed.key
    prefix = app.config['REDIS_QUEUE_KEY'] + ':result:'
    if key.startswith(prefix):
        key = key[len(prefix):]

    status_url = url_for('webmention_status', key=key, _external=True)
    return make_response(
        'Webmention queued for processing. Check status: {}'.format(status_url),
        202)


@app.route('/webmention/status/<key>')
def webmention_status(key):
    key = app.config['REDIS_QUEUE_KEY'] + ':result:' + key
    delayed = queue.DelayedResult(key)
    rv = delayed.return_value
    if not rv:
        return jsonify({
            'status': 202,
            'reason': 'Mention has not been processed or status has expired'
        })
    return jsonify(rv)


@queue.queueable
def process_webmention(source, target, callback):
    def call_callback(result):
        if callback:
            requests.post(callback, data=result)
    try:
        target_post, mention_url, delete, error \
            = do_process_webmention(source, target)

        if error or not target_post or not mention_url:
            app.logger.warn("Failed to process webmention: %s", error)
            result = {
                'source': source,
                'target': target,
                'status': 400,
                'reason': error
            }
            call_callback(result)
            return result

        with Post.writeable(target_post.path) as writeable_post:
            if delete:
                writeable_post.mentions.remove(mention_url)
            elif mention_url not in writeable_post.mentions:
                writeable_post.mentions.append(mention_url)
            writeable_post.save()
            app.logger.debug("saved mentions to %s", writeable_post.path)

        with Metadata.writeable() as mdata:
            mdata.insert_recent_mention(target_post, mention_url)
            mdata.save()

        push.handle_new_mentions()

        result = {
            'source': source,
            'target': target,
            'status': 200,
            'reason': 'Deleted' if delete else 'Created'
        }
        call_callback(result)
        return result

    except Exception as e:
        app.logger.exception("exception while processing webmention")
        result = {
            'source': source,
            'target': target,
            'status': 400,
            'reason': "exception while processing webmention {}".format(e)
        }
        call_callback(result)
        return result


def do_process_webmention(source, target):
    app.logger.debug("processing webmention from %s to %s", source, target)
    if target and target.strip('/') == app.config['SITE_URL'].strip('/'):
        # received a domain-level mention
        app.logger.debug('received domain-level webmention from %s', source)
        target_post = None
        target_urls = (target,)
        # TODO save domain-level webmention somewhere
    else:
        # confirm that target is a valid link to a post
        target_post = find_target_post(target)

        if not target_post:
            app.logger.warn(
                "Webmention could not find target post: %s. Giving up", target)
            return None, None, None, False, \
                "Webmention could not find target post: {}".format(target)

        target_urls = (target, target_post.permalink, target_post.short_permalink)

    # confirm that source actually refers to the post
    source_response = requests.get(source)
    app.logger.debug('received response from source %s', source_response)

    if source_response.status_code == 410:
        app.logger.debug("Webmention indicates original was deleted")
        return target_post, source, True, None

    if source_response.status_code // 100 != 2:
        app.logger.warn(
            "Webmention could not read source post: %s. Giving up", source)
        return target_post, None, False, \
            "Bad response when reading source post: {}, {}"\
            .format(source, source_response)

    source_length = source_response.headers.get('Content-Length')

    if source_length and int(source_length) > 2097152:
        app.logger.warn("Very large source. length=%s", source_length)
        return target_post, None, False,\
            "Source is very large. Length={}"\
            .format(source_length)

    link_to_target = find_link_to_target(source, source_response, target_urls)
    if not link_to_target:
        app.logger.warn(
            "Webmention source %s does not appear to link to target %s. "
            "Giving up", source, target)
        return target_post, None, False,\
            "Could not find any links from source to target"

    archiver.archive_html(source, source_response.text)

    return target_post, source, False, None


def find_link_to_target(source_url, source_response, target_urls):
    if source_response.status_code // 2 != 100:
        app.logger.warn(
            "Received unexpected response from webmention source: %s",
            source_response.text)
        return None

    # Don't worry about Microformats for now; just see if there is a
    # link anywhere that points back to the target
    soup = BeautifulSoup(source_response.text)
    for link in soup.find_all(['a', 'link']):
        link_target = link.get('href')
        if link_target in target_urls:
            return link


def find_target_post(target_url):
    app.logger.debug("looking for target post at %s", target_url)

    # follow redirects if necessary
    redirect_url = urllib.request.urlopen(target_url).geturl()
    if redirect_url and redirect_url != target_url:
        app.logger.debug("followed redirection to %s", redirect_url)
        target_url = redirect_url

    parsed_url = urllib.parse.urlparse(target_url)

    if not parsed_url:
        app.logger.warn(
            "Could not parse target_url of received webmention: %s",
            target_url)
        return None

    try:
        urls = app.url_map.bind(app.config['SITE_URL'])
        endpoint, args = urls.match(parsed_url.path)
    except NotFound:
        app.logger.warn("Webmention could not find target for %s",
                        parsed_url.path)
        return None

    if endpoint == 'post_by_date':
        post_type = args.get('post_type')
        year = args.get('year')
        month = args.get('month')
        day = args.get('day')
        index = args.get('index')
        post = Post.load_by_date(post_type, year, month, day, index)

    elif endpoint == 'post_by_old_date':
        post_type = args.get('post_type')
        yymmdd = args.get('yymmdd')
        year = int('20' + yymmdd[0:2])
        month = int(yymmdd[2:4])
        day = int(yymmdd[4:6])
        post = Post.load_by_date(post_type, year, month, day, index)

    elif endpoint == 'post_by_id':
        dbid = args.get('dbid')
        post = Post.load_by_id(dbid)

    if not post:
        app.logger.warn(
            "Webmention target points to unknown post: {}".format(args)),

    return post
