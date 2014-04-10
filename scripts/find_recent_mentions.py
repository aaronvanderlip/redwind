import os
import json
from redwind.models import Post, Mention


if __name__ == '__main__':
    mentions = []
    for root, dirs, files in os.walk('redwind/_data/posts'):
        for filename in files:
            post = Post.load(os.path.join(root, filename))
            for mention in post.mentions:
                mentions.append((post, mention))

    def sortkey(pair):
        post, mention = pair
        return mention.pub_date, post.pub_date

    mentions.sort(key=sortkey, reverse=True)

    recent_mentions = []
    for pair in mentions[:10]:
        post, mention = pair
        obj = {
            'post': {
                'title': post.title or post.content,
                'permalink': post.permalink
            },
            'mention': mention.to_json()
        }
        recent_mentions.append(obj)

    print(json.dumps(recent_mentions, indent=True))
