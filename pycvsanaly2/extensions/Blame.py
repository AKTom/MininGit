# Copyright (C) 2009 LibreSoft
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Authors :
#       Carlos Garcia Campos  <carlosgc@gsyc.escet.urjc.es>

from pycvsanaly2.Database import (SqliteDatabase, MysqlDatabase, 
    TableAlreadyExists, statement, ICursor)
from pycvsanaly2.extensions import (Extension, register_extension, 
    ExtensionRunError)
from pycvsanaly2.profile import profiler_start, profiler_stop
from pycvsanaly2.utils import printdbg, printerr, uri_to_filename, to_utf8
from FileRevs import FileRevs
from Jobs import JobPool, Job
from repositoryhandler.backends import RepositoryCommandError
from repositoryhandler.backends.watchers import BLAME
from guilty.parser import create_parser
from guilty.outputdevs import OutputDevice
import os


class BlameJob(Job):

    class BlameContentHandler(OutputDevice):
        def __init__(self):
            self.authors = {}

        def start_file(self, filename):
            pass

        def line(self, line):
            lauthor = to_utf8(line.author).decode("utf-8")
            self.authors.setdefault(lauthor, 0)
            self.authors[lauthor] += 1

        def end_file(self):
            pass

        def get_authors(self):
            return self.authors

    def __init__(self, file_id, commit_id, path, rev):
        Job.__init__(self)
        self.file_id = file_id
        self.commit_id = commit_id
        self.path = path
        self.rev = rev
        self.authors = None

    def run(self, repo, repo_uri):
        profiler_start("Running BlameJob for %s@%s", (self.path, self.rev))
        
        def blame_line(line, p):
            p.feed(line)

        repo_type = repo.get_type()
        if repo_type == 'cvs':
            # CVS paths contain the module stuff
            uri = repo.get_uri_for_path(repo_uri)
            module = uri[len(repo.get_uri()):].strip('/')

            if module != '.':
                path = self.path[len(module):].strip('/')
            else:
                path = self.path.strip('/')
        else:
            path = self.path.strip('/')

        filename = os.path.basename(self.path)
        p = create_parser(repo.get_type(), self.path)
        out = self.get_content_handler()
        p.set_output_device(out)
        wid = repo.add_watch(BLAME, blame_line, p)
        try:
            repo.blame(os.path.join(repo_uri, path), self.rev)
            self.collect_results(out)
        except RepositoryCommandError, e:
            self.failed = True
            printerr("Command %s returned %d (%s)", 
                     (e.cmd, e.returncode, e.error))
        p.end()
        repo.remove_watch(BLAME, wid)
        profiler_stop("Running BlameJob for %s@%s", 
                      (self.path, self.rev), delete=True)

    def collect_results(self, content_handler):
        self.authors = content_handler.get_authors()
        
    def get_content_handler(self):
        return self.BlameContentHandler()

    def get_authors(self):
        return self.authors

    def get_file_id(self):
        return self.file_id

    def get_commit_id(self):
        return self.commit_id


class Blame(Extension):

    deps = ['FileTypes']

    # Insert query
    __insert__ = """INSERT INTO blame (id, file_id, commit_id, author_id, 
                                       n_lines)
                         VALUES (?,?,?,?,?)"""
    MAX_BLAMES = 10
    
    job_class = BlameJob

    def __init__(self):
        self.db = None
        self.blames = []
        self.authors = None
        self.id_counter = 1

    def create_table(self, cnn):
        cursor = cnn.cursor()

        if isinstance(self.db, SqliteDatabase):
            import sqlite3.dbapi2

            try:
                cursor.execute("""CREATE TABLE blame (
                                id integer primary key,
                                file_id integer,
                                commit_id integer,
                                author_id integer,
                                n_lines integer
                                )""")
            except sqlite3.dbapi2.OperationalError:
                cursor.close()
                raise TableAlreadyExists
            except:
                raise
        elif isinstance(self.db, MysqlDatabase):
            import MySQLdb
            
            try:
                cursor.execute("""CREATE TABLE blame (
                                id integer primary key not null,
                                file_id integer,
                                commit_id integer,
                                author_id integer,
                                n_lines integer,
                                FOREIGN KEY (file_id) REFERENCES tree(id),
                                FOREIGN KEY (commit_id) REFERENCES scmlog(id),
                                FOREIGN KEY (author_id) REFERENCES people(id)
                                ) CHARACTER SET=utf8""")
            except MySQLdb.OperationalError, e:
                if e.args[0] == 1050:
                    cursor.close()
                    raise TableAlreadyExists
                raise
            except:
                raise

        cnn.commit()
        cursor.close()

    def get_blames(self, cursor, repoid):
        query = "select b.file_id, b.commit_id from blame b, files f " + \
                "where b.file_id = f.id and repository_id = ?"
        cursor.execute(statement(query, self.db.place_holder), (repoid,))
        return [(res[0], res[1]) for res in cursor.fetchall()]

    def __get_authors(self, cursor):
        query = "select id, name from people"
        cursor.execute(statement(query, self.db.place_holder))
        self.authors = dict([(name.decode("utf-8"), id) for id, name in cursor.fetchall()])

    def process_finished_jobs(self, job_pool, write_cursor, unlocked=False):
        if unlocked:
            job = job_pool.get_next_done_unlocked()
        else:
            job = job_pool.get_next_done(0.5)

        args = []
        
        processed_jobs = 0
        while job is not None:
            if not job.failed:
                a = self.populate_insert_args(job)
                args.extend(a)
                self.id_counter += len(a)
            processed_jobs += 1
            if unlocked:
                job = job_pool.get_next_done_unlocked()
            else:
                job = job_pool.get_next_done(0)

        if len(args) > 0:
            write_cursor.executemany(statement(self.__insert__, 
                                               self.db.place_holder), args)
            del args
        return processed_jobs

    def populate_insert_args(self, job):
        authors = job.get_authors()
        file_id = job.get_file_id()
        commit_id = job.get_commit_id()

        try:
            args = [(self.id_counter + i, file_id, commit_id, \
                 self.authors[key], authors[key]) \
                 for i, key in enumerate(authors.keys())]
        except:
            printdbg("Error occurred while processing file %d @ commit %d", (file_id, commit_id))
            raise
        return args
    
    def get_max_id(self, db):
        cnn = self.db.connect()
        cursor = cnn.cursor()
        cursor.execute(statement("SELECT max(id) from blame", 
                                 db.place_holder))
        max_id = cursor.fetchone()[0]
        cursor.close()
        cnn.close()
        return max_id

    def run(self, repo, uri, db):
        profiler_start("Running Blame extension")

        self.db = db

        cnn = self.db.connect()
        read_cursor = cnn.cursor()
        write_cursor = cnn.cursor()

        blames = []

        try:
            path = uri_to_filename(uri)
            if path is not None:
                repo_uri = repo.get_uri_for_path(path)
            else:
                repo_uri = uri

            read_cursor.execute(statement("SELECT id from repositories " + \
                                          "where uri = ?", db.place_holder), 
                                          (repo_uri,))
            repoid = read_cursor.fetchone()[0]
        except NotImplementedError:
            raise ExtensionRunError("Blame extension is not supported for " + \
                                    "%s repositories" % (repo.get_type()))
        except Exception, e:
            raise ExtensionRunError("Error creating repository %s. " + \
                                    "Exception: %s" % (repo.get_uri(), str(e)))

        try:
            self.create_table(cnn)
        except TableAlreadyExists:
            max_id = self.get_max_id(db)
            
            if max_id is not None:
                self.id_counter = max_id + 1

            
        except Exception, e:
            raise ExtensionRunError(str(e))

        self.__get_authors(read_cursor)

        if self.id_counter > 1:
            blames = self.get_blames(read_cursor, repoid)

        job_pool = JobPool(repo, path or repo.get_uri(), queuesize=100)

        # Get code files
        query = "select f.id from file_types ft, files f " + \
                "where f.id = ft.file_id and " + \
                "ft.type in ('code', 'unknown') and " + \
                "f.repository_id = ?"
        read_cursor.execute(statement(query, db.place_holder), (repoid,))
        code_files = [item[0] for item in read_cursor.fetchall()]

        n_blames = 0
        fr = FileRevs(db, cnn, read_cursor, repoid)
        for revision, commit_id, file_id, action_type, composed in fr:
            if file_id not in code_files:
                continue

            if(file_id, commit_id) in blames:
                printdbg("%d@%d is already in the database, skip it", 
                         (file_id, commit_id))
                continue

            if composed:
                rev = revision.split("|")[0]
            else:
                rev = revision

            relative_path = fr.get_path()
            printdbg("Path for %d at %s -> %s", (file_id, rev, relative_path))

            if repo.get_type() == 'svn' and relative_path == 'tags':
                printdbg("Skipping file %s", (relative_path,))
                continue

            job = self.job_class(file_id, commit_id, relative_path, rev)
            job_pool.push(job)
            n_blames += 1

            if n_blames >= self.MAX_BLAMES:
                job_pool.join()
                self.process_finished_jobs(job_pool, write_cursor)
                n_blames = 0
        job_pool.join()
        self.process_finished_jobs(job_pool, write_cursor, True)

        read_cursor.close()
        write_cursor.close()
        cnn.close()

        profiler_stop("Running Blame extension", delete=True)

    def backout(self, repo, uri, db):
        update_statement = """delete from blame where
                              commit_id in (select s.id from scmlog s
                                          where s.repository_id = ?)"""

        self._do_backout(repo, uri, db, update_statement)

register_extension("Blame", Blame)
