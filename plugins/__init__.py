#!/usr/bin/env python

###
# Copyright (c) 2002-2004, Jeremiah Fincher
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Redistributions of source code must retain the above copyright notice,
#     this list of conditions, and the following disclaimer.
#   * Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions, and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#   * Neither the name of the author of this software nor the name of
#     contributors to this software may be used to endorse or promote products
#     derived from this software without specific prior written consent.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
###

__revision__ = "$Id$"

import supybot.fix as fix

import gc
import os
import re
import csv
import sys
import math
import sets
import time
import random
import os.path
import UserDict
import threading

import supybot.cdb as cdb
import supybot.log as log
import supybot.conf as conf
import supybot.utils as utils
import supybot.world as world
import supybot.ircutils as ircutils
import supybot.webutils as webutils

try:
    mxCrap = {}
    for (name, module) in sys.modules.items():
        if name.startswith('mx'):
            mxCrap[name] = module
            sys.modules[name] = None
    import sqlite
    for (name, module) in mxCrap.items():
        sys.modules[name] = module
    sqlite.have_datetime = False
    Connection = sqlite.Connection
    class MyConnection(sqlite.Connection):
        def commit(self, *args, **kwargs):
            if self.autocommit:
                return
            else:
                Connection.commit(self, *args, **kwargs)
    sqlite.Connection = MyConnection
except ImportError:
    pass

class DBHandler(object):
    def __init__(self, name=None, suffix='.db'):
        if name is None:
            self.name = self.__class__.__name__
        else:
            self.name = name
        if suffix and suffix[0] != '.':
            suffix = '.' + suffix
        self.suffix = suffix
        self.cachedDb = None

    def makeFilename(self):
        if self.name.endswith(self.suffix):
            return self.name
        else:
            return self.name + self.suffix

    def makeDb(self, filename):
        raise NotImplementedError

    def getDb(self):
        if self.cachedDb is None or \
           threading.currentThread() is not world.mainThread:
            db = self.makeDb(self.makeFilename())
        else:
            db = self.cachedDb
        db.autocommit = 1
        return db

    def die(self):
        if self.cachedDb is not None:
            self.cachedDb.die()
            del self.cachedDb


def makeChannelFilename(channel, filename, dirname=None):
    assert ircutils.isChannel(channel), 'channel not a channel, ' \
           'the arguments to makeChannelFilename are probably reversed.'
    assert filename == os.path.basename(filename), 'We don\'t handle dirs.'
    channel = ircutils.toLower(channel)
    if conf.supybot.databases.plugins.channelSpecific.get(channel)():
        if dirname is None:
            dir = conf.supybot.directories.data.dirize(channel)
        else:
            dir = os.path.join(dirname, channel)
        if not os.path.exists(dir):
            os.makedirs(dir)
        return os.path.join(dir, filename)
    else:
        return conf.supybot.directories.data.dirize(filename)


# XXX: This shouldn't be a mixin.  This should be contained by classes that
#      want such behavior.  But at this point, it wouldn't gain much for us
#      to refactor it.
class ChannelDBHandler(object):
    """A class to handle database stuff for individual channels transparently.
    """
    suffix = '.db'
    def __init__(self, suffix='.db'):
        self.dbCache = ircutils.IrcDict()
        suffix = self.suffix
        if self.suffix and self.suffix[0] != '.':
            suffix = '.' + suffix
        self.suffix = suffix

    def makeFilename(self, channel):
        """Override this to specialize the filenames of your databases."""
        channel = ircutils.toLower(channel)
        className = self.__class__.__name__
        return makeChannelFilename(channel, className + self.suffix)

    def makeDb(self, filename):
        """Override this to create your databases."""
        return cdb.shelf(filename)

    def getDb(self, channel):
        """Use this to get a database for a specific channel."""
        currentThread = threading.currentThread()
        if channel not in self.dbCache and currentThread == world.mainThread:
            self.dbCache[channel] = self.makeDb(self.makeFilename(channel))
        if currentThread != world.mainThread:
            db = self.makeDb(self.makeFilename(channel))
        else:
            db = self.dbCache[channel]
        db.autocommit = 1
        return db

    def die(self):
        for db in self.dbCache.itervalues():
            try:
                db.commit()
            except AttributeError: # In case it's not an SQLite database.
                pass
            try:
                db.close()
            except AttributeError: # In case it doesn't have a close method.
                pass
            del db
        gc.collect()


class ChannelUserDictionary(UserDict.DictMixin):
    IdDict = dict
    def __init__(self):
        self.channels = ircutils.IrcDict()

    def __getitem__(self, (channel, id)):
        return self.channels[channel][id]

    def __setitem__(self, (channel, id), v):
        if channel not in self.channels:
            self.channels[channel] = self.IdDict()
        self.channels[channel][id] = v

    def __delitem__(self, (channel, id)):
        del self.channels[channel][id]

    def iteritems(self):
        for (channel, ids) in self.channels.iteritems():
            for (id, v) in ids.iteritems():
                yield ((channel, id), v)

    def keys(self):
        L = []
        for (k, _) in self.iteritems():
            L.append(k)
        return L


class ChannelUserDB(ChannelUserDictionary):
    def __init__(self, filename):
        ChannelUserDictionary.__init__(self)
        self.filename = filename
        try:
            fd = file(self.filename)
        except EnvironmentError, e:
            log.warning('Couldn\'t open %s: %s.', self.filename, e)
            return
        reader = csv.reader(fd)
        try:
            lineno = 0
            for t in reader:
                lineno += 1
                try:
                    channel = t.pop(0)
                    id = t.pop(0)
                    try:
                        id = int(id)
                    except ValueError:
                        # We'll skip over this so, say, nicks can be kept here.
                        pass
                    v = self.deserialize(channel, id, t)
                    self[channel, id] = v
                except Exception, e:
                    log.warning('Invalid line #%s in %s.',
                                lineno, self.__class__.__name__)
                    log.debug('Exception: %s', utils.exnToString(e))
        except Exception, e: # This catches exceptions from csv.reader.
            log.warning('Invalid line #%s in %s.',
                        lineno, self.__class__.__name__)
            log.debug('Exception: %s', utils.exnToString(e))

    def flush(self):
        fd = utils.transactionalFile(self.filename)
        writer = csv.writer(fd)
        items = self.items()
        if not items:
            log.warning('%s: Refusing to write blank file.',
                        self.__class__.__name__)
            fd.rollback()
            return
        items.sort()
        for ((channel, id), v) in items:
            L = self.serialize(v)
            L.insert(0, id)
            L.insert(0, channel)
            writer.writerow(L)
        fd.close()

    def close(self):
        self.flush()
        self.clear()

    def deserialize(self, channel, id, L):
        """Should take a list of strings and return an object to be accessed
        via self.get(channel, id)."""
        raise NotImplementedError

    def serialize(self, x):
        """Should take an object (as returned by self.get(channel, id)) and
        return a list (of any type serializable to csv)."""
        raise NotImplementedError

class PeriodicFileDownloader(object):
    """A class to periodically download a file/files.

    A class-level dictionary 'periodicFiles' maps names of files to
    three-tuples of
    (url, seconds between downloads, function to run with downloaded file).

    'url' should be in some form that urllib2.urlopen can handle (do note that
    urllib2.urlopen handles file:// links perfectly well.)

    'seconds between downloads' is the number of seconds between downloads,
    obviously.  An important point to remember, however, is that it is only
    engaged when a command is run.  I.e., if you say you want the file
    downloaded every day, but no commands that use it are run in a week, the
    next time such a command is run, it'll be using a week-old file.  If you
    don't want such behavior, you'll have to give an error mess age to the user
    and tell him to call you back in the morning.

    'function to run with downloaded file' is a function that will be passed
    a string *filename* of the downloaded file.  This will be some random
    filename probably generated via some mktemp-type-thing.  You can do what
    you want with this; you may want to build a database, take some stats,
    or simply rename the file.  You can pass None as your function and the
    file with automatically be renamed to match the filename you have it listed
    under.  It'll be in conf.supybot.directories.data, of course.

    Aside from that dictionary, simply use self.getFile(filename) in any method
    that makes use of a periodically downloaded file, and you'll be set.
    """
    periodicFiles = None
    def __init__(self):
        if self.periodicFiles is None:
            raise ValueError, 'You must provide files to download'
        self.lastDownloaded = {}
        self.downloadedCounter = {}
        for filename in self.periodicFiles:
            if self.periodicFiles[filename][-1] is None:
                fullname = os.path.join(conf.supybot.directories.data(),
                                        filename)
                if os.path.exists(fullname):
                    self.lastDownloaded[filename] = os.stat(fullname).st_ctime
                else:
                    self.lastDownloaded[filename] = 0
            else:
                self.lastDownloaded[filename] = 0
            self.currentlyDownloading = sets.Set()
            self.downloadedCounter[filename] = 0
            self.getFile(filename)

    def _downloadFile(self, filename, url, f):
        self.currentlyDownloading.add(filename)
        try:
            try:
                infd = webutils.getUrlFd(url)
            except IOError, e:
                self.log.warning('Error downloading %s: %s', url, e)
                return
            except webutils.WebError, e:
                self.log.warning('Error downloading %s: %s', url, e)
                return
            confDir = conf.supybot.directories.data()
            newFilename = os.path.join(confDir, utils.mktemp())
            outfd = file(newFilename, 'wb')
            start = time.time()
            s = infd.read(4096)
            while s:
                outfd.write(s)
                s = infd.read(4096)
            infd.close()
            outfd.close()
            self.log.info('Downloaded %s in %s seconds',
                          filename, time.time()-start)
            self.downloadedCounter[filename] += 1
            self.lastDownloaded[filename] = time.time()
            if f is None:
                toFilename = os.path.join(confDir, filename)
                if os.name == 'nt':
                    # Windows, grrr...
                    if os.path.exists(toFilename):
                        os.remove(toFilename)
                os.rename(newFilename, toFilename)
            else:
                start = time.time()
                f(newFilename)
                total = time.time() - start
                self.log.info('Function ran on %s in %s seconds',
                              filename, total)
        finally:
            self.currentlyDownloading.remove(filename)

    def getFile(self, filename):
        if world.documenting:
            return
        (url, timeLimit, f) = self.periodicFiles[filename]
        if time.time() - self.lastDownloaded[filename] > timeLimit and \
           filename not in self.currentlyDownloading:
            self.log.info('Beginning download of %s', url)
            args = (filename, url, f)
            name = '%s #%s' % (filename, self.downloadedCounter[filename])
            t = threading.Thread(target=self._downloadFile, name=name,
                                 args=(filename, url, f))
            t.setDaemon(True)
            t.start()
            world.threadsSpawned += 1


def standardSubstitute(irc, msg, text, env=None):
    """Do the standard set of substitutions on text, and return it"""
    if ircutils.isChannel(msg.args[0]):
        channel = msg.args[0]
    else:
        channel = 'somewhere'
    def randInt():
        return str(random.randint(-1000, 1000))
    def randDate():
        t = pow(2,30)*random.random()+time.time()/4.0
        return time.ctime(t)
    def randNick():
        if channel != 'somewhere':
            return random.choice(list(irc.state.channels[channel].users))
        else:
            return 'someone'
    ctime = time.ctime()
    localtime = time.localtime()
    vars = ircutils.IrcDict({
        'who': msg.nick,
        'nick': msg.nick,
        'user': msg.user,
        'host': msg.host,
        'channel': channel,
        'botnick': irc.nick,
        'now': ctime, 'ctime': ctime,
        'randnick': randNick, 'randomnick': randNick,
        'randdate': randDate, 'randomdate': randDate,
        'rand': randInt, 'randint': randInt, 'randomint': randInt,
        'today': time.strftime('%d %b %Y', localtime),
        'year': localtime[0],
        'month': localtime[1],
        'monthname': time.strftime('%b', localtime),
        'date': localtime[2],
        'day': time.strftime('%A', localtime),
        'h': localtime[3], 'hr': localtime[3], 'hour': localtime[3],
        'm': localtime[4], 'min': localtime[4], 'minute': localtime[4],
        's': localtime[5], 'sec': localtime[5], 'second': localtime[5],
        'tz': time.tzname[time.daylight],
        })
    if env is not None:
        vars.update(env)
    return utils.perlVariableSubstitute(vars, text)


# vim:set shiftwidth=4 tabstop=8 expandtab textwidth=78:
