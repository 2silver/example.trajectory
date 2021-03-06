from copy import copy
from sqlalchemy.engine.url import make_url
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from datetime import datetime
from datetime import time
import logging
from threading import RLock
from threading import local
import os

from ZPublisher.interfaces import IPubFailure
from ZPublisher.interfaces import IPubStart
from ZPublisher.interfaces import IPubSuccess
from example.trajectory.interfaces import IDatabaseLoginOptions
from example.trajectory.models import Base
from pytz import UTC
from sqlalchemy import create_engine
from sqlalchemy import types
from sqlalchemy.orm import scoped_session
from sqlalchemy.orm import sessionmaker
from zope.component import adapter
from zope.component import getGlobalSiteManager
from zope.component import provideHandler
from zope.interface import implements


# quickly turn on datbase debugging
DEBUG = False

# configure logging through python logger insteda of the default
# sqlalchemy who knows logger
logging.basicConfig()

if DEBUG:
    logging.getLogger('sqlalchemy.engine').setLevel(logging.DEBUG)
    logging.getLogger('sqlalchemy.pool').setLevel(logging.DEBUG)

_DB_CONFIG_LOCK = RLock()

_PROFILE_SESSION = None
_PROFILE_ENGINE = None
_SQLA = local()


def _create_database_if_missing(dsn):
    url = copy(make_url(dsn))
    database = url.database
    url.database = 'template1'
    engine = create_engine(url)
    query = "SELECT 1 FROM pg_database WHERE datname='%s'" % database
    exists = bool(engine.execute(query).scalar())
    if not exists:
        engine.raw_connection().set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        query = "CREATE DATABASE {0} ENCODING '{1}' TEMPLATE {2}".format(
            database,
            'utf8',
            'template0'
        )
        engine.execute(query)


def getProfileSession():
    if not _PROFILE_SESSION:
        initializeSqlIntegration()
    return _PROFILE_SESSION


def getProfileEngine():
    if not _PROFILE_ENGINE:
        initializeSqlIntegration()
    return _PROFILE_ENGINE


def initializeSqlIntegration():
    gsm = getGlobalSiteManager()
    dbconfig = gsm.queryUtility(IDatabaseLoginOptions)
    if not dbconfig:
        raise Exception(
            "Could not lookup database dsn. Please check configuration")

    # Enforce UTC as our base timezone. See UTCDateTime class below.
    os.putenv("PGTZ", "UTC")

    '''
    eleddy: since we moved this code out of the actual init, so that we can
    have a sane configuration setup, we MUST do this with a lock. Otherwise sql
    alchemy loses its shit and goes on a 'fuck you muli-threading - I'll eat
    pancakes on your grave!' tirade. Then you spend your friday
    night sober and staring at an abyss of configuration headaches and
    infinite loops usually reserved for Mondays.
    '''

    with _DB_CONFIG_LOCK:
        global _PROFILE_ENGINE
        global _PROFILE_SESSION
        global DEBUG

        if not _PROFILE_ENGINE:
            _create_database_if_missing(dbconfig.dsn)
            _PROFILE_ENGINE = create_engine(dbconfig.dsn,
                                            pool_size=5,
                                            pool_recycle=3600,
                                            convert_unicode=True,
                                            echo=DEBUG,
                                            )
            Base.metadata.create_all(_PROFILE_ENGINE)

        if not _PROFILE_SESSION:
            _PROFILE_SESSION = scoped_session(
                sessionmaker(bind=_PROFILE_ENGINE))


def initializeSession():
    """
    Create a session local to a thread.

    Also note that for some reason, in 4.2, this access is happening
    from the dummy startup thread, and not the main thread. I have no
    idea how this will affect things long term.
    """
    _SQLA.session = getProfileSession()


def getSession():
    """
    """
    # XXX: we can check for errors and what not here later
    # e.g. if a transaction needs rolled back or something
    if not getattr(_SQLA, 'session', None):
        initializeSession()
    return _SQLA.session


def closeSession():
    session = getSession()
    if session:
        session.close()


@adapter(IPubStart)
def configureSessionOnStart(event):
    initializeSession()


@adapter(IPubSuccess)
def persistSessionOnSuccess(event):
    closeSession()


@adapter(IPubFailure)
def persistSessionOnFailure(event):
    if not event.retry:
        closeSession()


# setup and tear down sessions in the request object
provideHandler(configureSessionOnStart)
provideHandler(persistSessionOnSuccess)
provideHandler(persistSessionOnFailure)


class TestDatabaseLogin(object):
    implements(IDatabaseLoginOptions)
    dsn = "postgresql://example:example!@localhost/exampledb_testing"


class LocalDatabaseLogin(object):
    implements(IDatabaseLoginOptions)
    dsn = "postgresql://example:example!@localhost/exampledb"


class UTCDateTime(types.TypeDecorator):
    """A datetime type that stores only UTC datetimes.

    PostgreSQL does this wonderful thing where it automatically converts
    all dateimes to the server-local timezone. Which, is ok, I suppose,
    but this is a web application and our database server isn't always
    in the same location as our users. So, we force Postgres to use UTC as
    its timezone and then enforce saving all datetimes as UTC.

    Timezones are fun.
    """

    impl = types.DateTime

    def process_bind_param(self, value, engine):
        if value is not None:
            # If we're passed a date, convert it to a datetime, at time 00:00
            if not hasattr(value, 'tzinfo'):
                return UTC.localize(datetime.combine(value, time()))
            try:
                return value.astimezone(UTC)
            except ValueError:
                raise ValueError(
                    "UTCDateTime field was passed a non-UTC datetime")

    def process_result_value(self, value, engine):
        if value is not None:
            return UTC.localize(datetime(value.year,
                                         value.month,
                                         value.day,
                                         value.hour,
                                         value.minute,
                                         value.second,
                                         value.microsecond))


def utcnow():
    return UTC.localize(datetime.now())
