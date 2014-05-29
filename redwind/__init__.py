import sys
if 'mf2py' not in sys.path:
    sys.path.append('mf2py')

from flask import Flask
#from flask_debugtoolbar import DebugToolbarExtension
from werkzeug.datastructures import ImmutableDict
from redis import Redis

app = Flask(__name__)
app.config.from_object('config.Configuration')

redis = Redis()

app.jinja_options = ImmutableDict(
    trim_blocks=True,
    lstrip_blocks=True,
    extensions=['jinja2.ext.autoescape', 'jinja2.ext.with_', 'jinja2.ext.i18n']
)

#toolbar = DebugToolbarExtension(app)

if not app.debug:
    import logging
    from logging.handlers import RotatingFileHandler
    app.logger.setLevel(logging.DEBUG)

    file_handler = RotatingFileHandler('app.log', maxBytes=1048576,
                                       backupCount=5)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    file_handler.setFormatter(formatter)
    app.logger.addHandler(file_handler)


from . import views
