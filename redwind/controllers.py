from . import app
from . import api
from . import archiver
from . import auth
from . import contexts
from . import facebook
from . import locations
from . import push
from . import twitter
from . import util
from . import wm_sender
from . import wm_receiver
from .models import Post, Location, Metadata, AddressBook, POST_TYPES

from bs4 import BeautifulSoup
from flask import request, redirect, url_for, render_template, flash,\
    abort, make_response, Markup, send_from_directory
from flask.ext.login import login_required, login_user, logout_user,\
    current_user
from contextlib import contextmanager
import urllib.parse
from werkzeug.routing import BaseConverter
import jinja2.filters

import bleach
import collections
import datetime
import mf2util
import operator
import os
import pytz
import re
import requests
import unicodedata

bleach.ALLOWED_TAGS += ['img', 'p', 'br']
bleach.ALLOWED_ATTRIBUTES.update({
    'img': ['src', 'alt', 'title']
})

TIMEZONE = pytz.timezone('US/Pacific')

POST_TYPE_RULE = '<any(' + ','.join(POST_TYPES) + '):post_type>'
DATE_RULE = '<int:year>/<int(fixed_digits=2):month>/'\
            '<int(fixed_digits=2):day>/<index>'

TWITTER_PROFILE_RE = re.compile(r'https?://(?:www\.)?twitter\.com/(\w+)')
TWITTER_RE = twitter.TwitterClient.PERMALINK_RE
FACEBOOK_PROFILE_RE = re.compile(r'https?://(?:www\.)?facebook\.com/([a-zA-Z0-9._-]+)')
FACEBOOK_RE = re.compile(r'https?://(?:www\.)?facebook\.com/([a-zA-Z0-9._-]+)/\w+/(\w+)')
YOUTUBE_RE = re.compile(r'https?://(?:www.)?youtube\.com/watch\?v=(\w+)')
INSTAGRAM_RE = re.compile(r'https?://instagram\.com/p/(\w+)')

AUTHOR_PLACEHOLDER = 'img/users/placeholder.png'

def reraise_attribute_errors(func):
    """@property and my override of getattr don't mix — they swallow up
    AttributeErrors with no log messages, so I need this ugly hack to
    turn them into RuntimeErrors
    """
    def go(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except AttributeError as e:
            raise RuntimeError(e)
    return go


DPost = collections.namedtuple('DPost', [
    'post_type',
    'draft',
    'permalink',
    'short_permalink',
    'short_cite',
    'shortid',
    'reply_contexts',
    'share_contexts',
    'like_contexts',
    'title',
    'content',
    'content_plain',
    'first_image',
    'pub_date_iso',
    'pub_date_human',
    'pub_day',
    'location',
    'location_url',
    'syndication',
    'tags',
    'audience',
    'reply_url',
    'retweet_url',
    'favorite_url',
    'likes',
    'reposts',
    'replies',
    'references',
    'like_count',
    'repost_count',
    'reply_count',
    'reference_count'
])


DContext = collections.namedtuple('DContext', [
    'url',
    'permalink',
    'author_name',
    'author_url',
    'author_image',
    'content',
    'repost_preview',
    'pub_date',
    'pub_date_iso',
    'pub_date_human',
    'title',
    'deleted',
])

DMention = collections.namedtuple('DMention', [
    'permalink',
    'reftype',
    'author_name',
    'author_url',
    'author_image',
    'content',
    'content_plain',
    'content_words',
    'pub_date',
    'pub_date_iso',
    'pub_date_human',
    'title',
    'deleted',
    'syndication',
    'children',
])


def create_dpost(post):

    def get_pub_date(mention):
        result = mention.pub_date
        if not result:
            result = datetime.datetime(1982, 11, 24, tzinfo=pytz.utc)
        elif result and hasattr(result, 'tzinfo') and not result.tzinfo:
            result = pytz.utc.localize(result)
        return result

    def mentions_sorted_by_date(mentions, mtype):
        filtered = [m for m in mentions
                    if not m.deleted
                    and (not mtype or m.reftype == mtype)]
        filtered.sort(key=get_pub_date)
        return filtered

    content = Markup(markdown_filter(
        post.content, img_path=post.get_image_path()))

    # arrange posse'd mentions into a hierarchy based on rel-syndication
    mentions = []
    all_mentions = [create_dmention(post, m) for m in post.mentions]
    for mention in all_mentions:
        parent = next((parent for parent in all_mentions
                       if mention != parent
                       and any(util.urls_match(mention.permalink, synd)
                               for synd in parent.syndication)),
                      None)
        if parent:
            parent.children.append(mention)
        else:
            mentions.append(mention)

    likes = mentions_sorted_by_date(mentions, 'like')
    reposts = mentions_sorted_by_date(mentions, 'repost')
    replies = mentions_sorted_by_date(mentions, 'reply')
    references = mentions_sorted_by_date(mentions, 'reference')

    # find the first image (if any) that is in an <img> tag
    # in the rendered post
    soup = BeautifulSoup(content)
    first_image = None
    for img in soup.find_all('img'):
        app.logger.debug('checking image %s', img)
        hcard_parent = img.find_parent(class_='h-card')
        if not hcard_parent:
            src = img.get('src')
            if src:
                first_image = urllib.parse.urljoin(app.config['SITE_URL'], src)
                break

    tweet_id = None
    for url in post.syndication:
        match = TWITTER_RE.match(url)
        if match:
            tweet_id = match.group(2)
            break

    reply_url = tweet_id and 'https://twitter.com/intent/tweet?in_reply_to={}'.format(tweet_id)
    retweet_url = tweet_id and 'https://twitter.com/intent/retweet?tweet_id={}'.format(tweet_id)
    favorite_url = tweet_id and 'https://twitter.com/intent/favorite?tweet_id={}'.format(tweet_id)
    location_url = post.location and 'http://www.openstreetmap.org/#map=11/{}/{}'.format(
        post.location.approximate_latitude,
        post.location.approximate_longitude)

    return DPost(
        post_type=post.post_type,
        draft=post.draft,
        permalink=post.permalink,
        short_permalink=post.short_permalink,
        short_cite=post.short_cite,
        shortid=post.shortid,

        reply_contexts=[create_dcontext(url) for url in post.in_reply_to],
        share_contexts=[create_dcontext(url) for url in post.repost_of],
        like_contexts=[create_dcontext(url) for url in post.like_of],
        title=post.title,

        content=content,
        content_plain=format_as_text(content),
        first_image=first_image,

        pub_date_iso=isotime_filter(post.pub_date),
        pub_date_human=human_time(post.pub_date),
        pub_day=post.pub_date and post.pub_date.strftime('%Y-%m-%d'),

        location=post.location,
        location_url=location_url,
        syndication=[format_syndication_url(s) for s in post.syndication],
        tags=post.tags,
        audience=post.audience,

        reply_url=reply_url,
        retweet_url=retweet_url,
        favorite_url=favorite_url,

        likes=likes,
        reposts=reposts,
        replies=replies,
        references=references,
        like_count=len(likes),
        repost_count=len(reposts),
        reply_count=len(replies),
        reference_count=len(references)
    )


def create_dcontext(url):
    repost_preview = None
    # youtube embeds
    m = YOUTUBE_RE.match(url)
    if m:
        repost_preview = (
            """<iframe width="560" height="315" """
            """src="//www.youtube.com/embed/{}" frameborder="0" """
            """allowfullscreen></iframe>"""
            .format(m.group(1)))

    # instagram embeds
    m = INSTAGRAM_RE.match(url)
    if m:
        repost_preview = (
            """<iframe src="//instagram.com/p/{}/embed/" """
            """width="400" height="500" frameborder="0" scrolling="no" """
            """allowtransparency="true"></iframe>"""
            .format(m.group(1)))

    blob = archiver.load_json_from_archive(url)
    if blob:
        try:
            entry = mf2util.interpret(blob, url)
            pub_date = entry.get('published')

            content = entry.get('content', '')
            content_plain = format_as_text(content)

            if len(content_plain) < 512:
                content = bleach.clean(autolink(content), strip=True)
            else:
                content = (
                    jinja2.filters.do_truncate(content_plain, 512) +
                    ' <a class="u-url" href="{}">continued</a>'.format(url))

            title = entry.get('name', 'a post')
            if len(title) > 256:
                title = jinja2.filters.do_truncate(title, 256)

            author_name = bleach.clean(entry.get('author', {}).get('name', ''))
            author_image = entry.get('author', {}).get('photo')
            if author_image:
                author_image = local_mirror_resource(author_image)

            return DContext(
                url=url,
                permalink=entry.get('url', url),
                author_name=author_name,
                author_url=entry.get('author', {}).get('url', ''),
                author_image=author_image or url_for(
                    'static', filename=AUTHOR_PLACEHOLDER),
                content=content,
                repost_preview=repost_preview,
                pub_date=pub_date,
                pub_date_iso=isotime_filter(pub_date),
                pub_date_human=human_time(pub_date),
                title=title,
                deleted=False,
            )
        except:
            app.logger.exception('error interpreting %s', url)

    return DContext(
        url=url,
        permalink=url,
        author_name=None,
        author_url=None,
        author_image=None,
        content=None,
        repost_preview=repost_preview,
        pub_date=None,
        pub_date_iso=None,
        pub_date_human=None,
        title='a post',
        deleted=False,
    )


def create_dmention(post, url):
    target_urls = [
        post.permalink,
        post.permalink_without_slug,
        post.short_permalink,
        post.permalink.replace('https://', 'http://'),
        post.permalink_without_slug.replace('https://', 'http://'),
        post.short_permalink.replace('https://', 'http://'),
    ] if post else []

    try:
        blob = archiver.load_json_from_archive(url)
        if blob:
            entry = mf2util.interpret_comment(blob, url, target_urls)
            if entry:
                comment_type = entry.get('comment_type')

                content = entry.get('content', '')
                content_plain = format_as_text(content)
                content_words = jinja2.filters.do_wordcount(content_plain)

                author_name = bleach.clean(
                    entry.get('author', {}).get('name', ''))
                author_image = entry.get('author', {}).get('photo')
                if author_image:
                    author_image = local_mirror_resource(author_image)

                return DMention(
                    permalink=entry.get('url', ''),
                    reftype=comment_type and comment_type[0],
                    author_name=author_name,
                    author_url=entry.get('author', {}).get('url', ''),
                    author_image=author_image or url_for(
                        'static', filename=AUTHOR_PLACEHOLDER),
                    content=content,
                    content_plain=content_plain,
                    content_words=content_words,
                    pub_date=entry.get('published'),
                    pub_date_iso=isotime_filter(entry.get('published')),
                    pub_date_human=human_time(entry.get('published')),
                    title=entry.get('name'),
                    deleted=False,
                    syndication=[format_syndication_url(s, False) for s
                                 in entry.get('syndication', [])],
                    children=[]
                )

    except:
        app.logger.exception('error interpreting {}', url)

    return DMention(
        permalink=url,
        reftype='reference',
        author_name=None,
        author_url=None,
        author_image=None,
        content=None,
        content_plain=None,
        content_words=0,
        pub_date=None,
        pub_date_iso=None,
        pub_date_human=None,
        title=None,
        deleted=False,
        syndication=[],
        children=[]
    )


def render_posts(title, post_types, page, per_page, tag=None,
                 include_hidden=False, include_drafts=False):
    mdata = Metadata()
    posts = mdata.load_posts(reverse=True, post_types=post_types, tag=tag,
                             include_hidden=include_hidden,
                             include_drafts=include_drafts,
                             page=page, per_page=per_page)

    dposts = [create_dpost(post) for post in posts if check_audience(post)]
    return render_template('posts.html', posts=dposts, title=title,
                           prev_page=page-1,
                           next_page=page+1)


@app.route('/', defaults={'page': 1})
@app.route('/page/<int:page>')
def index(page):
    # leave out hidden posts
    return render_posts(None, POST_TYPES, page, 30,
                        include_hidden=False,
                        include_drafts=current_user.is_authenticated())


@app.route('/articles', defaults={'page': 1})
@app.route('/articles/page/<int:page>')
def articles(page):
    return render_posts('All Articles', ('article',), page, 10,
                        include_hidden=False,
                        include_drafts=current_user.is_authenticated())


@app.route('/checkins', defaults={'page': 1})
@app.route('/checkins/page/<int:page>')
def checkins(page):
    return render_posts('All Check-ins', ('checkin',), page, 10,
                        include_hidden=False,
                        include_drafts=current_user.is_authenticated())


@app.route('/everything', defaults={'page': 1})
@app.route('/everything/page/<int:page>')
def everything(page):
    return render_posts('Everything', POST_TYPES, page, 30,
                        include_hidden=True,
                        include_drafts=current_user.is_authenticated())


@app.route('/tag/<tag>', defaults={'page': 1})
@app.route('/tag/<tag>/page/<int:page>')
def posts_by_tag(tag, page):
    return render_posts('All posts tagged ' + tag, POST_TYPES, page, 30, tag=tag,
                        include_hidden=True,
                        include_drafts=current_user.is_authenticated())


def render_posts_atom(title, feed_id, post_types, count):
    mdata = Metadata()
    posts = mdata.load_posts(reverse=True, post_types=post_types,
                             page=1, per_page=10)
    dposts = [create_dpost(post) for post in posts if check_audience(post)]
    return make_response(render_template('posts.atom', title=title,
                                         feed_id=feed_id,
                                         posts=dposts), 200,
                         {'Content-Type': 'application/atom+xml; charset=utf-8'})


@app.route("/all.atom")
def all_atom():
    return render_posts_atom('All', 'all.atom', None, 30)


@app.route("/updates.atom")
def updates_atom():
    return render_posts_atom('Updates', 'updates.atom',
                             ('article', 'note', 'share'), 30)


@app.route("/articles.atom")
def articles_atom():
    return render_posts_atom('Articles', 'articles.atom', ('article',), 10)


@app.route("/mentions.atom")
def mentions_atom():
    mdata = Metadata()
    mentions = mdata.get_recent_mentions()
    proxies = []
    for mention in mentions:
        post_path = mention.get('post')
        post = Post.load(post_path) if post_path else None
        mention_url = mention.get('mention')
        proxies.append(create_dmention(post, mention_url))
    return make_response(render_template('mentions.atom',
                                         title='kylewm.com: Mentions',
                                         feed_id='mentions.atom',
                                         mentions=proxies), 200,
                         {'Content-Type': 'application/atom+xml'})


@app.route('/archive', defaults={'year': None, 'month': None})
@app.route('/archive/<int:year>/<int(fixed_digits=2):month>')
def archive(year, month):
    # give the template the posts from this month,
    # and the list of all years/month
    posts = []
    if year and month:
        posts = [create_dpost(post) for post
                 in Metadata().load_by_month(year, month)
                 if check_audience(post)]

    years = Metadata().get_archive_months()
    return render_template(
        'archive.html', years=years,
        expanded_year=year, expanded_month=month,
        posts=posts)


def check_audience(post):
    if not post.audience:
        # all posts public by default
        return True

    if current_user.is_authenticated():
        # admin user can see everything
        return True

    if current_user.is_anonymous():
        # anonymous users can't see stuff
        return False

    # check that their username is listed in the post's audience
    app.logger.debug('checking that logged in user %s is in post audience %s',
                     current_user.get_id(), post.audience)
    return current_user.get_id() in post.audience


@app.route('/{}/{}/files/<filename>'.format(POST_TYPE_RULE, DATE_RULE))
def post_associated_file(post_type, year, month, day, index, filename):
    post = Post.load_by_date(post_type, year, month, day, index)
    if not post:
        abort(404)

    if post.deleted:
        abort(410)  # deleted permanently

    if not check_audience(post):
        abort(401)  # not authorized TODO a nicer page

    resourcedir = os.path.join(app.root_path, '_data', post.path, 'files')

    size = request.args.get('size')
    if size == 'small':
        resourcedir, filename = util.resize_image(resourcedir, filename, 300)
    elif size == 'medium':
        resourcedir, filename = util.resize_image(resourcedir, filename, 800)
    elif size == 'large':
        resourcedir, filename = util.resize_image(resourcedir, filename, 1024)

    _, ext = os.path.splitext(filename)
    return send_from_directory(resourcedir, filename,
                               mimetype='text/plain' if ext == '.md' else None)


@app.route('/' + POST_TYPE_RULE + '/' + DATE_RULE, defaults={'slug': None})
@app.route('/' + POST_TYPE_RULE + '/' + DATE_RULE + '/<slug>')
def post_by_date(post_type, year, month, day, index, slug):
    post = Post.load_by_date(post_type, year, month, day, index)
    if not post or (post.draft and not current_user.is_authenticated()):
        abort(404)

    if post.deleted:
        abort(410)  # deleted permanently

    if not check_audience(post):
        abort(401)  # not authorized TODO a nicer page

    if not slug and post.slug:
        return redirect(
            url_for('post_by_date', post_type=post_type,
                    year=year, month=month, day=day, index=index,
                    slug=post.slug))

    dpost = create_dpost(post)
    title = dpost.title
    if not title:
        title = "A {} from {}".format(dpost.post_type, dpost.pub_day)
    return render_template('post.html', post=dpost, title=title)


@app.route('/short/<string(minlength=5,maxlength=6):tag>')
def shortlink(tag):
    post_type = util.parse_type(tag)
    pub_date = util.parse_date(tag)
    index = util.parse_index(tag)

    if not post_type or not pub_date or not index:
        abort(404)

    return redirect(url_for('post_by_date', post_type=post_type,
                            year=pub_date.year, month=pub_date.month,
                            day=pub_date.day, index=index))


# for testing -- allows arbitrary logins as any user
# @app.route('/fakeauth')
# def fakeauth():
#     domain = request.args.get('url')
#     user = auth.load_user(domain)
#     login_user(user, remember=True)
#     return redirect(url_for('index'))


@app.route("/indieauth")
def indieauth():
    token = request.args.get('token')
    response = requests.get('https://indieauth.com/verify',
                            params={'token': token})

    if response.status_code == 200:
        domain = response.json().get('me')
        user = auth.load_user(urllib.parse.urlparse(domain).netloc)
        if user:
            login_user(user, remember=True)
            flash('Logged in with domain {}'.format(domain))
        else:
            flash('No user for domain {}'.format(domain))

    else:
        respjson = response.json()
        flash('Login failed {}: {}'.format(respjson.get('error'),
                                           respjson.get('error_description')))

    return redirect(url_for('index'))


@app.route("/admin/logout")
def logout():
    logout_user()
    return redirect(url_for('index'))


@app.route('/admin/settings')
@login_required
def settings():
    return render_template("settings.html", user=current_user)


@app.route('/admin/delete')
@login_required
def delete_by_id():
    shortid = request.args.get('id')
    with Post.writeable(Post.shortid_to_path(shortid)) as post:
        if not post:
            abort(404)
        post.deleted = True
        post.save()

    with Metadata.writeable() as mdata:
        mdata.add_or_update_post(post)
        mdata.save()

    redirect_url = request.args.get('redirect') or url_for('index')
    app.logger.debug("redirecting to {}".format(redirect_url))
    return redirect(redirect_url)


@app.route('/admin/new')
def new_post():
    post_type = request.args.get('type', 'note')
    post = Post(post_type)
    post.content = ''

    if post_type == 'reply':
        in_reply_to = request.args.get('in_reply_to')
        if in_reply_to:
            post.in_reply_to = [in_reply_to]

    if post_type == 'share':
        repost_of = request.args.get('repost_of')
        if repost_of:
            post.repost_of = [repost_of]

    if post_type == 'like':
        post.hidden = True
        like_of = request.args.get('like_of')
        if like_of:
            post.like_of = [like_of]

    content = request.args.get('content')
    if content:
        post.content = content

    return render_template('edit_post.html', edit_type='new', post=post,
                           advanced=request.args.get('advanced'))


@app.route('/admin/edit')
def edit_by_id():
    shortid = request.args.get('id')
    post = Post.load_by_shortid(shortid)

    if not post:
        abort(404)
    return render_template('edit_post.html', edit_type='edit', post=post,
                           advanced=request.args.get('advanced'))


@app.route('/admin/uploads')
def uploads_popup():
    return render_template('uploads_popup.html')


def isotime_filter(thedate):
    if not thedate:
        thedate = datetime.date(1982, 11, 24)

    if hasattr(thedate, 'tzinfo') and not thedate.tzinfo:
        tz = pytz.timezone(app.config['TIMEZONE'])
        thedate = pytz.utc.localize(thedate).astimezone(tz)

    if isinstance(thedate, datetime.datetime):
        return thedate.isoformat('T')
    return thedate.isoformat()


def human_time(thedate, alternate=None):
    if not thedate:
        return alternate

    if hasattr(thedate, 'tzinfo') and not thedate.tzinfo:
        tz = pytz.timezone(app.config['TIMEZONE'])
        thedate = pytz.utc.localize(thedate).astimezone(tz)

    #return thedate.strftime('%B %-d, %Y %-I:%M%P %Z')

    if datetime.datetime.now(TIMEZONE) - thedate > datetime.timedelta(days=1):
        return thedate.strftime('%B %-d, %Y')
    else:
        return thedate.strftime('%B %-d, %Y %-I:%M%P %Z')

@app.template_filter('pluralize')
def pluralize(number, singular='', plural='s'):
    if number == 1:
        return singular
    else:
        return plural


@app.template_filter('month_shortname')
def month_shortname(month):
    return datetime.date(1990, month, 1).strftime('%b')


@app.template_filter('month_name')
def month_name(month):
    return datetime.date(1990, month, 1).strftime('%B')


def url_for_other_page(page):
    args = request.view_args.copy()
    args['page'] = page
    return url_for(request.endpoint, **args)

app.jinja_env.globals['url_for_other_page'] = url_for_other_page


@app.template_filter('atom_sanitize')
def atom_sanitize(content):
    return Markup.escape(str(content))


def person_to_microcard(fullname, displayname, entry, pos):
    url = entry.get('url')
    photo = entry.get('photo')
    if url and photo:
        photo_mirror = local_mirror_resource(photo)
        return '<a class="microcard h-card" href="{}">'\
            '<img src="{}" />{}</a>'.format(url, photo_mirror, displayname)
    return displayname


def process_people(data, person_processor):
    book = None

    regex = re.compile(r"\[\[([\w ]+)(?:\|([\w\-'. ]+))?\]\]")
    start = 0
    while True:
        m = regex.search(data, start)
        if not m:
            break
        if not book:
            book = AddressBook()
        fullname = m.group(1)
        displayname = m.group(2) or fullname
        replacement = person_processor(fullname, displayname,
                                       book.entries.get(fullname, {}),
                                       m.start())
        data = data[:m.start()] + replacement + data[m.end():]
        start = m.start() + len(replacement)
    return data


def markdown_filter(data, img_path=None, link_twitter_names=True,
                    person_processor=person_to_microcard):
    from markdown import markdown
    from smartypants import smartypants

    if img_path:
        # replace relative paths to images with absolute
        data = re.sub(
            '(?<!\\\)!\[([^\]]*)\]\(([^/)]+)\)',
            '![\g<1>](' + img_path + '/\g<2>)', data)

    if person_processor:
        data = process_people(data, person_processor)

    result = markdown(data, extensions=['codehilite', 'fenced_code'])
    result = util.autolink(result, twitter_names=link_twitter_names)
    result = smartypants(result)
    return result


def format_as_text(html, remove_imgs=True):
    soup = BeautifulSoup(html)

    # replace links with the URL
    for a in soup.find_all('a'):
        a.replace_with(a.get('href') or 'link')
    # and images with their alt text
    for i in soup.find_all('img'):
        if remove_imgs:
            i.hidden = True
        else:
            i.replace_with('[{}]'.format(i.get('title')
                                         or i.get('alt')
                                         or 'image'))
    return soup.get_text().strip()


@app.template_filter('autolink')
def autolink(plain, twitter_names=True):
    return util.autolink(plain, twitter_names)


@app.template_filter('prettify_url')
def prettify_url(url):
    if not url:
        return url
    split = url.split('//', 1)
    if len(split) == 2:
        schema, path = split
    else:
        path = url
    return path.strip('/')


@app.template_filter('domain_from_url')
def domain_from_url(url):
    if not url:
        return url
    return urllib.parse.urlparse(url).netloc


def format_syndication_url(url, include_rel=True):
    fmt = '<a class="u-syndication" '
    if include_rel:
        fmt += 'rel="syndication" '
    fmt += 'href="{}"><i class="fa {}"></i> {}</a>'

    if TWITTER_RE.match(url):
        return fmt.format(url, 'fa-twitter', 'Twitter')
    if FACEBOOK_RE.match(url):
        return fmt.format(url, 'fa-facebook', 'Facebook')
    if INSTAGRAM_RE.match(url):
        return fmt.format(url, 'fa-instagram', 'Instagram')

    return fmt.format(url, 'fa-paper-plane', prettify_url(url))


def local_mirror_resource(url):
    site_netloc = urllib.parse.urlparse(app.config['SITE_URL']).netloc

    o = urllib.parse.urlparse(url)
    if o.netloc and o.netloc != site_netloc:
        mirror_url_path = os.path.join("_mirror", o.netloc, o.path.strip('/'))
        mirror_file_path = os.path.join(app.root_path, app.static_folder,
                                        mirror_url_path)
        app.logger.debug("checking for existence of mirrored resource %s -> %s",
                         url, mirror_file_path)
        if os.path.exists(mirror_file_path):
            app.logger.debug("%s already mirrored, returning url path %s",
                             mirror_file_path, mirror_url_path)
            return url_for('static', filename=mirror_url_path)

        if util.download_resource(url, mirror_file_path):
            app.logger.debug("%s did not exist, successfully mirrored to %s.",
                             mirror_file_path, mirror_url_path)
            return url_for('static', filename=mirror_url_path)
        else:
            app.logger.warn("failed to download %s to %s for some reason", url,
                            mirror_file_path)

    return url


@app.route('/admin/save_edit', methods=['POST'])
@login_required
def save_edit():
    shortid = request.form.get('post_id')
    app.logger.debug("saving post %s", shortid)
    with Post.writeable(Post.shortid_to_path(shortid)) as post:
        return save_post(post)


@app.route('/admin/save_new', methods=['POST'])
@login_required
def save_new():
    post_type = request.form.get('post_type', 'note')
    app.logger.debug("saving new post of type %s", post_type)
    post = Post(post_type)
    try:
        post._writeable = True
        return save_post(post)
    finally:
        post._writeable = False


def save_post(post):
    def slugify(s):
        slug = unicodedata.normalize('NFKD', s).lower()
        slug = re.sub(r'[^a-z0-9]+', '-', slug).strip('-')
        slug = re.sub(r'[-]+', '-', slug)
        return slug[:256]

    try:
        app.logger.debug("acquired write lock %s", post)

        # populate the Post object and save it to the database,
        # redirect to the view
        post.title = request.form.get('title', '')
        post.content = request.form.get('content')

        post.draft = request.form.get('draft', 'false') == 'true'
        post.hidden = request.form.get('hidden', 'false') == 'true'

        lat = request.form.get('latitude')
        lon = request.form.get('longitude')
        if lat and lon:
            post.location = Location(latitude=float(lat),
                                     longitude=float(lon),
                                     name=request.form.get('location_name'))
        else:
            post.location = None

        if not post.pub_date:
            post.pub_date = datetime.datetime.utcnow()
        post.reserve_date_index()

        slug = request.form.get('slug')
        if slug:
            post.slug = slug
        elif post.title and not post.slug:
            post.slug = slugify(post.title)

        in_reply_to = request.form.get('in_reply_to', '')
        post.in_reply_to = [url.strip() for url
                            in in_reply_to.split('\n')
                            if url.strip()]

        repost_of = request.form.get('repost_of', '')
        post.repost_of = [url.strip() for url
                          in repost_of.split('\n')
                          if url.strip()]

        like_of = request.form.get('like_of', '')
        post.like_of = [url.strip() for url
                        in like_of.split('\n')
                        if url.strip()]

        syndication = request.form.get('syndication', '')
        post.syndication = [url.strip() for url in
                            syndication.split('\n')
                            if url.strip()]

        audience = request.form.get('audience', '')
        post.audience = [url.strip() for url in
                         audience.split('\n')
                         if url.strip()]

        post.tags = request.form.get('tags', '').split()

        file_to_url = {}
        infiles = request.files.getlist('files')
        app.logger.debug('infiles: %s', infiles)
        for infile in infiles:
            if infile and infile.filename:
                app.logger.debug('receiving uploaded file %s', infile)
                relpath, photo_url, fullpath \
                    = api.generate_upload_path(post, infile)
                if not os.path.exists(os.path.dirname(fullpath)):
                    os.makedirs(os.path.dirname(fullpath))
                infile.save(fullpath)
                file_to_url[infile] = photo_url

        app.logger.debug('uploaded files map %s', file_to_url)

        post.save()

        with Metadata.writeable() as mdata:
            mdata.add_or_update_post(post)
            mdata.save()

        app.logger.debug("saved post %s %s", post.shortid, post.permalink)
        redirect_url = post.permalink

        try:
            app.logger.debug("fetching contexts")
            contexts.fetch_post_contexts(post)

            app.logger.debug("fetching location info")
            locations.reverse_geocode(post)

            if request.form.get('send_push') == 'true' and not post.draft:
                app.logger.debug("sending push notification")
                push.send_notifications(post)

            if request.form.get('send_webmentions') == 'true' and not post.draft:
                app.logger.debug("sending webmentions")
                wm_sender.send_webmentions(post)
        except:
            app.logger.exception("exception while dispatching queued tasks")

        return redirect(redirect_url)

    except Exception as e:
        app.logger.exception("Failed to save post")
        flash('failed to save post {}'.format(e))

        return redirect(url_for('index'))


@app.route('/admin/addressbook', methods=['GET', 'POST'])
def addressbook():
    book = AddressBook()

    if request.method == 'POST':
        if not current_user.is_authenticated():
            return app.login_manager.unauthorized()

        person = request.form.get('person').strip()
        url = request.form.get('url').strip()
        photo = request.form.get('photo').strip()
        twitter_name = request.form.get('twitter').strip()
        facebook_id = request.form.get('facebook').strip()

        book.entries[person] = {
            'url': url,
            'photo': photo,
            'twitter': twitter_name,
            'facebook': facebook_id,
        }

        book.save()
        return redirect(url_for('addressbook'))

    return render_template('addressbook.html', entries=book.entries)
