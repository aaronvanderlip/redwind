from . import app
from . import queue
from .models import Post
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from urllib.request import urlopen, Request

import re
import requests


def send_webmentions(post):
    try:
        app.logger.debug("queueing webmentions for {}".format(post.shortid))
        do_send_webmentions.delay(post.shortid)
        return True, 'Success'

    except Exception as e:
        app.logger.exception('sending webmentions')
        return False, "Exception while sending webmention: {}"\
            .format(e)


@queue.queueable
def do_send_webmentions(post_id):
    app.logger.debug("sending mentions for {}".format(post_id))
    post = Post.load_by_shortid(post_id)
    return mention_client.handle_new_or_edit(post)


class MentionClient:
    def __init__(self):
        self.cached_responses = {}

    def get_source_url(self, post):
        return post.permalink

    def get_target_urls(self, post):
        from .controllers import create_dpost

        target_urls = []
        # send mentions to 'in_reply_to' as well as all linked urls
        target_urls += post.in_reply_to
        target_urls += post.repost_of
        target_urls += post.like_of

        dpost = create_dpost(post)
        app.logger.debug("search post content {}".format(dpost.content))

        soup = BeautifulSoup(dpost.content)
        for link in soup.find_all('a'):
            link_target = link.get('href')
            if link_target:
                app.logger.debug("found link {} with href {}"
                                 .format(link, link_target))
                target_urls.append(link_target.strip())

        return target_urls

    def get_response(self, url):
        if url in self.cached_responses:
            return self.cached_responses[url]
        response = requests.get(url)
        self.cached_responses[url] = response
        return response

    def handle_new_or_edit(self, post):
        target_urls = self.get_target_urls(post)
        app.logger.debug("Sending webmentions to these urls {}"
                         .format(" ; ".join(target_urls)))
        results = []
        for target_url in target_urls:
            results.append(self.send_mention(post, target_url))
        return results

    def send_mention(self, post, target_url):
        app.logger.debug("Looking for webmention endpoint on %s",
                         target_url)

        success, explanation = self.check_content_type_and_size(target_url)
        if success:
            if self.supports_webmention(target_url):
                app.logger.debug("Site supports webmention")
                success, explanation = self.send_webmention(post, target_url)

            elif self.supports_pingback(target_url):
                app.logger.debug("Site supports pingback")
                success, explanation = self.send_pingback(post, target_url)
                app.logger.debug("Sending pingback successful: %s", success)

            else:
                app.logger.debug("Site does not support mentions")
                success = False
                explanation = 'Site does not support webmentions or pingbacks'

        return {'target': target_url,
                'success': success,
                'explanation': explanation}

    def check_content_type_and_size(self, target_url):
        request = Request(target_url, headers={'User-Agent': 'kylewm.com'})
        metadata = urlopen(request).info()
        if not metadata:
            return False, "Could not retrieve metadata for url {}".format(
                target_url)

        content_type = metadata.get_content_maintype()
        content_length = metadata.get('Content-Length')

        if content_type and content_type != 'text':
            return False, "Target content type '{}' is not 'text'".format(
                content_type)

        if content_length and int(content_length) > 2097152:
            return False, "Target content length {} is too large".format(
                content_length)

        return True, None

    def supports_webmention(self, target_url):
        return self.find_webmention_endpoint(target_url) is not None

    def find_webmention_endpoint(self, target_url):
        app.logger.debug("looking for webmention endpoint in %s", target_url)
        response = self.get_response(target_url)
        app.logger.debug("looking for webmention endpoint in headers and body")
        endpoint = (self.find_webmention_endpoint_in_headers(response.headers)
                    or self.find_webmention_endpoint_in_html(response.text))
        app.logger.debug("webmention endpoint %s %s", response.url, endpoint)
        return endpoint and urljoin(response.url, endpoint)

    def find_webmention_endpoint_in_headers(self, headers):
        if 'link' in headers:
            m = re.search('<(https?://[^>]+)>; rel="webmention"',
                          headers.get('link')) or \
                re.search('<(https?://[^>]+)>; rel="http://webmention.org/?"',
                          headers.get('link'))
            if m:
                return m.group(1)

    def find_webmention_endpoint_in_html(self, body):
        soup = BeautifulSoup(body)
        link = (soup.find('link', attrs={'rel': 'webmention'})
                or soup.find('link', attrs={'rel': 'http://webmention.org/'})
                or soup.find('a', attrs={'rel': 'webmention'}))
        return link and link.get('href')

    def send_webmention(self, post, target_url):
        app.logger.debug(
            "Sending webmention from %s to %s",
            self.get_source_url(post), target_url)

        try:
            endpoint = self.find_webmention_endpoint(target_url)
            if not endpoint:
                return False, "No webmention endpoint for {}".format(
                    target_url)

            payload = {'source': self.get_source_url(post),
                       'target': target_url}
            headers = {'content-type': 'application/x-www-form-urlencoded',
                       'accept': 'application/json'}
            response = requests.post(endpoint, data=payload, headers=headers)

            #from https://github.com/vrypan/webmention-tools/blob/master/
            #             webmentiontools/send.py
            if response.status_code // 100 != 2:
                app.logger.warn(
                    "Failed to send webmention for %s. "
                    "Response status code: %s, %s",
                    target_url, response.status_code, response.text)
                return False, "Status code: {}, Response: {}".format(
                    response.status_code, response.text)
            else:
                app.logger.debug(
                    "Sent webmention successfully to %s. Sender response: %s:",
                    target_url, response.text)
                return True, "Successful"

        except Exception as e:
            return False, "Exception while sending webmention {}".format(e)

    def supports_pingback(self, target_url):
        return self.find_pingback_endpoint(target_url) is not None

    def find_pingback_endpoint(self, target_url):
        response = self.get_response(target_url)
        endpoint = response.headers.get('x-pingback')
        if not endpoint:
            soup = BeautifulSoup(response.text)
            link = soup.find('link', attrs={'rel': 'pingback'})
            endpoint = link and link.get('href')
        return endpoint

    def send_pingback(self, post, target_url):
        try:
            endpoint = self.find_pingback_endpoint(target_url)
            source_url = self.get_source_url(post)

            payload = (
                """<?xml version="1.0" encoding="iso-8859-1"?><methodCall>"""
                """<methodName>pingback.ping</methodName><params><param>"""
                """<value><string>{}</string></value></param><param><value>"""
                """<string>{}</string></value></param></params></methodCall>"""
                .format(source_url, target_url))
            headers = {'content-type': 'application/xml'}
            response = requests.post(endpoint, data=payload, headers=headers)
            app.logger.debug(
                "Pingback to %s response status code %s. Message %s",
                target_url, response.status_code, response.text)

            return True, "Sent pingback successfully"
        except Exception as e:
            return False, "Exception while sending pingback: {}".format(e)


mention_client = MentionClient()
