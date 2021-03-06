# encoding: utf-8

import warnings
import logging
import re
from datetime import datetime

import vdm.sqlalchemy
from vdm.sqlalchemy.base import SQLAlchemySession
from sqlalchemy import MetaData, __version__ as sqav, Table
from sqlalchemy.util import OrderedDict

import ckan.model.meta as meta
from ckan.model.meta import (
    Session,
    engine_is_sqlite,
    engine_is_pg,
)
from app import(
	App,
	app_table,
)

import ckan.migration

log = logging.getLogger(__name__)



# set up in init_model after metadata is bound
version_table = None


def init_model(engine):
    '''Call me before using any of the tables or classes in the model'''
    meta.Session.remove()
    meta.Session.configure(bind=engine)
    meta.create_local_session.configure(bind=engine)
    meta.engine = engine
    meta.metadata.bind = engine
    # sqlalchemy migrate version table
    import sqlalchemy.exc
    try:
        global version_table
        version_table = Table('migrate_version', meta.metadata, autoload=True)
    except sqlalchemy.exc.NoSuchTableError:
        pass


class Repository(vdm.sqlalchemy.Repository):
    migrate_repository = ckan.migration.__path__[0]

    # note: tables_created value is not sustained between instantiations
    #       so only useful for tests. The alternative is to use
    #       are_tables_created().
    tables_created_and_initialised = False

    def init_db(self):
        '''Ensures tables, const data and some default config is created.
        This method MUST be run before using CKAN for the first time.
        Before this method is run, you can either have a clean db or tables
        that may have been setup with either upgrade_db or a previous run of
        init_db.
        '''
        warnings.filterwarnings('ignore', 'SAWarning')
        self.session.rollback()
        self.session.remove()
        # sqlite database needs to be recreated each time as the
        # memory database is lost.
        if self.metadata.bind.name == 'sqlite':
            # this creates the tables, which isn't required inbetween tests
            # that have simply called rebuild_db.
            self.create_db()
        else:
            if not self.tables_created_and_initialised:
                self.upgrade_db()
                ## make sure celery tables are made as celery only makes
                ## them after adding a task
                try:
                    import ckan.lib.celery_app as celery_app
                    import celery.db.session as celery_session
                    import celery.backends.database
                    ## This creates the database tables (if using that backend)
                    ## It is a slight hack to celery
                    backend = celery_app.celery.backend
                    if isinstance(backend,
                                  celery.backends.database.DatabaseBackend):
                        celery_result_session = backend.ResultSession()
                        engine = celery_result_session.bind
                        celery_session.ResultModelBase.metadata.\
                            create_all(engine)
                except ImportError:
                    # use of celery is optional
                    pass

                self.tables_created_and_initialised = True
        log.info('Database initialised')

    def clean_db(self):
        self.commit_and_remove()
        meta.metadata = MetaData(self.metadata.bind)
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', '.*(reflection|tsvector).*')
            meta.metadata.reflect()

        meta.metadata.drop_all()
        self.tables_created_and_initialised = False
        log.info('Database tables dropped')

    def create_db(self):
        '''Ensures tables, const data and some default config is created.
        i.e. the same as init_db APART from when running tests, when init_db
        has shortcuts.
        '''
        self.metadata.create_all(bind=self.metadata.bind)
        log.info('Database tables created')

    def latest_migration_version(self):
        import migrate.versioning.api as mig
        version = mig.version(self.migrate_repository)
        return version

    def rebuild_db(self):
        '''Clean and init the db'''
        if self.tables_created_and_initialised:
            # just delete data, leaving tables - this is faster
            self.delete_all()
        else:
            # delete tables and data
            self.clean_db()
        self.session.remove()
        self.init_db()
        self.session.flush()
        log.info('Database rebuilt')

    def delete_all(self):
        '''Delete all data from all tables.'''
        self.session.remove()
        ## use raw connection for performance
        connection = self.session.connection()
        if sqav.startswith("0.4"):
            tables = self.metadata.table_iterator()
        else:
            tables = reversed(self.metadata.sorted_tables)
        for table in tables:
            if table.name == 'migrate_version':
                continue
            connection.execute('delete from "%s"' % table.name)
        self.session.commit()
        log.info('Database table data deleted')

    def setup_migration_version_control(self, version=None):
        import migrate.exceptions
        import migrate.versioning.api as mig
        # set up db version control (if not already)
        try:
            mig.version_control(self.metadata.bind,
                    self.migrate_repository, version)
        except migrate.exceptions.DatabaseAlreadyControlledError:
            pass

    def upgrade_db(self, version=None):
        '''Upgrade db using sqlalchemy migrations.

        @param version: version to upgrade to (if None upgrade to latest)
        '''
        assert meta.engine.name in ('postgres', 'postgresql'), \
            'Database migration - only Postgresql engine supported (not %s).' \
                % meta.engine.name
        import migrate.versioning.api as mig
        self.setup_migration_version_control()
        version_before = mig.db_version(self.metadata.bind, self.migrate_repository)
        mig.upgrade(self.metadata.bind, self.migrate_repository, version=version)
        version_after = mig.db_version(self.metadata.bind, self.migrate_repository)
        if version_after != version_before:
            log.info('CKAN database version upgraded: %s -> %s', version_before, version_after)
        else:
            log.info('CKAN database version remains as: %s', version_after)

        ##this prints the diffs in a readable format
        ##import pprint
        ##from migrate.versioning.schemadiff import getDiffOfModelAgainstDatabase
        ##pprint.pprint(getDiffOfModelAgainstDatabase(self.metadata, self.metadata.bind).colDiffs)

    def are_tables_created(self):
        meta.metadata = MetaData(self.metadata.bind)
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', '.*(reflection|geometry).*')
            meta.metadata.reflect()
        return bool(meta.metadata.tables)

    def purge_revision(self, revision, leave_record=False):
        '''Purge all changes associated with a revision.

        @param leave_record: if True leave revision in existence but
        change message to "PURGED: {date-time-of-purge}". If false
        delete revision object as well.

        Summary of the Algorithm
        ------------------------

        1. list all RevisionObjects affected by this revision
        2. check continuity objects and cascade on everything else ?
        3. crudely get all object revisions associated with this
        4. then check whether this is the only revision and delete
           the continuity object

        5. ALTERNATIVELY delete all associated object revisions then
           do a select on continutity to check which have zero
           associated revisions (should only be these ...) '''

        to_purge = []
        SQLAlchemySession.setattr(self.session, 'revisioning_disabled', True)
        self.session.autoflush = False
        for o in self.versioned_objects:
            revobj = o.__revision_class__
            items = self.session.query(revobj). \
                    filter_by(revision=revision).all()
            for item in items:
                continuity = item.continuity

                if continuity.revision == revision:  # must change continuity
                    trevobjs = self.session.query(revobj).join('revision'). \
                            filter(revobj.continuity == continuity). \
                            order_by(Revision.timestamp.desc()).all()
                    if len(trevobjs) == 0:
                        raise Exception('Should have at least one revision.')
                    if len(trevobjs) == 1:
                        to_purge.append(continuity)
                    else:
                        self.revert(continuity, trevobjs[1])
                        for num, obj in enumerate(trevobjs):
                            if num == 0:
                                continue

                            obj.expired_timestamp = datetime(9999, 12, 31)
                            self.session.add(obj)
                            break
                # now delete revision object
                self.session.delete(item)
            for cont in to_purge:
                self.session.delete(cont)
        if leave_record:
            revision.message = u'PURGED: %s' % datetime.now()
        else:
            self.session.delete(revision)
        self.commit_and_remove()


repo = Repository(meta.metadata, meta.Session)
