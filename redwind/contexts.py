from . import app
from . import archiver
from . import queue
from .twitter import twitter_client
import itertools


def fetch_post_contexts(post):
    for url in itertools.chain(post.in_reply_to, post.repost_of, post.like_of):
        do_fetch_context.delay(url)


@queue.queueable
def do_fetch_context(url):
    try:
        app.logger.debug("fetching url %s", url)
        fetch_external_post(url)
    except Exception as e:
        app.logger.exception("failure fetching contexts")


def fetch_external_post(url):
    app.logger.debug("checking twitter for %s", url)
    if twitter_client.fetch_external_post(url):
        return
    archiver.archive_url(url)
