#!/usr/bin/env python
#
# -*- coding: utf-8 -*-
#
# Copyright 2011 GRNET S.A. All rights reserved.
#
# Redistribution and use in source and binary forms, with or
# without modification, are permitted provided that the following
# conditions are met:
#
#   1. Redistributions of source code must retain the above
#      copyright notice, this list of conditions and the following
#      disclaimer.
#
#   2. Redistributions in binary form must reproduce the above
#      copyright notice, this list of conditions and the following
#      disclaimer in the documentation and/or other materials
#      provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY GRNET S.A. ``AS IS'' AND ANY EXPRESS
# OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL GRNET S.A OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF
# USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED
# AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and
# documentation are those of the authors and should not be
# interpreted as representing official policies, either expressed
# or implied, of GRNET S.A.
#
#

"""Unit Tests for the pool classes in pool

Provides unit tests for the code implementing pool
classes in the objpool module.

"""

# Support running under a gevent-monkey-patched environment
# if the "monkey" argument is specified in the command line.
import sys
if "monkey" in sys.argv:
    from gevent import monkey
    monkey.patch_all()
    sys.argv.pop(sys.argv.index("monkey"))

import sys
import time
import threading
from collections import defaultdict

from socket import socket, AF_INET, SOCK_STREAM, IPPROTO_TCP, SHUT_RDWR

from objpool import ObjectPool, PoolLimitError, PoolVerificationError
from objpool.http import PooledHTTPConnection, HTTPConnectionPool
from objpool.http import _pools as _http_pools

# Use backported unittest functionality if Python < 2.7
try:
    import unittest2 as unittest
except ImportError:
    if sys.version_info < (2, 7):
        raise Exception("The unittest2 package is required for Python < 2.7")
    import unittest


from threading import Lock

mutex = Lock()


class NumbersPool(ObjectPool):
    max = 0

    def _pool_create_safe(self):
        with mutex:
            n = self.max
            self.max += 1
        return n

    def _pool_create_unsafe(self):
        n = self.max
        self.max += 1
        return n

    # set this to _pool_create_unsafe to check
    # the thread-safety test
    #_pool_create = _pool_create_unsafe
    _pool_create = _pool_create_safe

    def _pool_verify(self, obj):
        return True

    def _pool_cleanup(self, obj):
        n = int(obj)
        if n < 0:
            return True
        return False


class ObjectPoolTestCase(unittest.TestCase):
    def test_create_pool_invalid_sizes(self):
        """Test __init__() requires valid size argument"""
        self.assertRaises(ValueError, ObjectPool, size=-1)
        self.assertRaises(ValueError, ObjectPool, size="size10")

    def test_create_pool_valid_sizes(self):
        ObjectPool(size=0)
        ObjectPool(size=None)

    def test_unbounded_pool(self):
        pool = ObjectPool(size=0, create=[1,2,3].pop)
        self.assertEqual(pool.pool_get(), 3)
        self.assertEqual(pool.pool_get(), 2)
        self.assertEqual(pool.pool_get(), 1)

    def test_create_pool(self):
        """Test pool creation works"""
        pool = ObjectPool(100)
        self.assertEqual(pool.size, 100)

    def test_get_not_implemented(self):
        """Test pool_get() method not implemented in abstract class"""
        pool = ObjectPool(100)
        self.assertRaises(NotImplementedError, pool._pool_create)

    def test_get_with_factory(self):
        obj_generator = iter(range(10)).next
        pool = ObjectPool(3, create=obj_generator)
        self.assertEqual(pool.pool_get(), 0)
        self.assertEqual(pool.pool_get(), 1)
        self.assertEqual(pool.pool_get(), 2)

    def test_put_with_factory(self):
        cleaned_objects = []
        pool = ObjectPool(3,
            create=[2, 1, 0].pop,
            verify=lambda o: o % 2 == 0,
            cleanup=cleaned_objects.append,
        )
        self.assertEqual(pool.pool_get(), 0)
        pool.pool_put(0)
        self.assertEqual(pool.pool_get(), 0)
        self.assertRaises(PoolVerificationError, pool.pool_get)
        self.assertEqual(pool.pool_get(), 2)
        self.assertEqual(cleaned_objects, [0])


class NumbersPoolTestCase(unittest.TestCase):
    N = 1500
    SEC = 0.5
    maxDiff = None

    def setUp(self):
        self.numbers = NumbersPool(self.N)

    def test_initially_empty(self):
        """Test pool is empty upon creation"""
        self.assertEqual(self.numbers._set, set([]))

    def test_seq_allocate_all(self):
        """Test allocation and deallocation of all pool objects"""
        n = []
        for _ in xrange(0, self.N):
            n.append(self.numbers.pool_get())
        self.assertEqual(n, range(0, self.N))
        for i in n:
            self.numbers.pool_put(i)
        self.assertEqual(self.numbers._set, set(n))

    def test_parallel_allocate_all(self):
        """Allocate all pool objects in parallel"""
        def allocate_one(pool, results, index):
            n = pool.pool_get()
            results[index] = n

        results = [None] * self.N
        threads = [threading.Thread(target=allocate_one,
                                    args=(self.numbers, results, i))
                   for i in xrange(0, self.N)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # This nonblocking pool_get() should fail
        self.assertRaises(PoolLimitError, self.numbers.pool_get,
                          blocking=False)
        self.assertEqual(sorted(results), range(0, self.N))

    def test_allocate_no_create(self):
        """Allocate objects from the pool without creating them"""
        for i in xrange(0, self.N):
            self.assertIsNone(self.numbers.pool_get(create=False))

        # This nonblocking pool_get() should fail
        self.assertRaises(PoolLimitError, self.numbers.pool_get,
                          blocking=False)

    def test_pool_cleanup_returns_failure(self):
        """Put a broken object, test a new one is retrieved eventually"""
        n = []
        for _ in xrange(0, self.N):
            n.append(self.numbers.pool_get())
        self.assertEqual(n, range(0, self.N))

        del n[-1:]
        self.numbers.pool_put(-1)  # This is a broken object
        self.assertFalse(self.numbers._set)
        self.assertEqual(self.numbers.pool_get(), self.N)

    def test_parallel_get_blocks(self):
        """Test threads block if no object left in the pool"""
        def allocate_one_and_sleep(pool, sec, result, index):
            n = pool.pool_get()
            time.sleep(sec)
            result[index] = n
            pool.pool_put(n)

        nr_threads = 2 * self.N + 1
        results = [None] * nr_threads
        threads = [threading.Thread(target=allocate_one_and_sleep,
                                    args=(self.numbers, self.SEC, results, i))
                   for i in xrange(nr_threads)]

        # This should take 3 * SEC seconds
        start = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        diff = time.time() - start
        self.assertTrue(diff > 3 * self.SEC)
        self.assertLess((diff - 3 * self.SEC) / 3 * self.SEC, .5)

        freq = defaultdict(int)
        for r in results:
            freq[r] += 1

        # The maximum number used must be exactly the pool size.
        self.assertEqual(max(results), self.N - 1)
        # At least one number must have been used three times
        triples = [r for r in freq if freq[r] == 3]
        self.assertGreater(len(triples), 0)
        # The sum of all frequencies must equal to the number of threads.
        self.assertEqual(sum(freq.values()), nr_threads)

    def test_verify_create(self):
        numbers = self.numbers
        nums = [numbers.pool_get() for _ in xrange(self.N)]
        for num in nums:
            numbers.pool_put(num)

        def verify(num):
            if num in nums:
                return False
            return True

        self.numbers._pool_verify = verify
        self.assertEqual(numbers.pool_get(), self.N)

    def test_verify_error(self):
        numbers = self.numbers
        nums = [numbers.pool_get() for _ in xrange(self.N)]
        for num in nums:
            numbers.pool_put(num)

        def false(*args):
            return False

        self.numbers._pool_verify = false
        self.assertRaises(PoolVerificationError, numbers.pool_get)

    def test_create_false(self):
        numpool = self.numbers
        for _ in xrange(self.N + 1):
            none = numpool.pool_get(create=False)
            self.assertEqual(none, None)
            numpool.pool_put(None)


class ThreadSafetyTestCase(unittest.TestCase):

    pool_class = NumbersPool

    def setUp(self):
        size = 3000
        self.size = size
        self.pool = self.pool_class(size)

    def test_parallel_sleeping_create(self):
        def create(pool, results, i):
            time.sleep(1)
            results[i] = pool._pool_create()

        pool = self.pool
        N = self.size
        results = [None] * N
        threads = [threading.Thread(target=create, args=(pool, results, i))
                   for i in xrange(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        freq = defaultdict(int)
        for r in results:
            freq[r] += 1

        mults = [(n, c) for n, c in freq.items() if c > 1]
        if mults:
            #print mults
            raise AssertionError("_pool_create() is not thread safe")


class TestHTTPConnectionTestCase(unittest.TestCase):
    def setUp(self):
        #netloc = "127.0.0.1:9999"
        #scheme='http'
        #self.pool = HTTPConnectionPool(
        #                netloc=netloc,
        #                scheme=scheme,
        #                pool_size=1)
        #key = (scheme, netloc)
        #_http_pools[key] = pool

        _http_pools.clear()

        self.host = "127.0.0.1"
        self.port = 9999
        self.netloc = "%s:%s" % (self.host, self.port)
        self.scheme = "http"
        self.key = (self.scheme, self.netloc)

        sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP)
        sock.bind((self.host, self.port))
        sock.listen(1)
        self.sock = sock

    def tearDown(self):
        sock = self.sock
        sock.shutdown(SHUT_RDWR)
        sock.close()

    def test_double_release(self):
        pooled = PooledHTTPConnection(self.netloc, self.scheme,
                                      pool_key='test_key')
        pooled.acquire()
        pool = pooled._pool
        cached_pool = _http_pools[("test_key", self.scheme, self.netloc)]
        self.assertTrue(pooled._pool is cached_pool)
        pooled.release()

        poolsize = len(pool._set)

        if PooledHTTPConnection._pool_disable_after_release:
            self.assertTrue(pooled._pool is False)

        if not PooledHTTPConnection._pool_ignore_double_release:
            with self.assertRaises(AssertionError):
                pooled.release()
        else:
            pooled.release()

        self.assertEqual(poolsize, len(pool._set))

    def test_distinct_pools_per_scheme(self):
        with PooledHTTPConnection("127.0.0.1", "http",
                                  attach_context=True, pool_key='test2') as conn:
            pool = conn._pool_context._pool
            self.assertTrue(pool is _http_pools[("test2", "http", "127.0.0.1")])

        with PooledHTTPConnection("127.0.0.1", "https",
                                  attach_context=True, pool_key='test2') as conn2:
            pool2 = conn2._pool_context._pool
            self.assertTrue(conn is not conn2)
            self.assertNotEqual(pool, pool2)
            self.assertTrue(pool2 is _http_pools[("test2", "https", "127.0.0.1")])

    def test_clean_connection(self):
        pool = None
        pooled = PooledHTTPConnection(self.netloc, self.scheme)
        conn = pooled.acquire()
        pool = pooled._pool
        self.assertTrue(pool is not None)
        pooled.release()
        self.assertTrue(pooled._pool is False)
        poolset = pool._set
        self.assertEqual(len(poolset), 1)
        pooled_conn = list(poolset)[0]
        self.assertTrue(pooled_conn is conn)

    def test_dirty_connection(self):
        pooled = PooledHTTPConnection(self.netloc, self.scheme)
        conn = pooled.acquire()
        pool = pooled._pool
        conn.request("GET", "/")
        serversock, addr = self.sock.accept()
        serversock.send("HTTP/1.1 200 OK\n"
                        "Content-Length: 6\n"
                        "\n"
                        "HELLO\n")
        time.sleep(0.3)
        # We would read this message like this
        #resp = conn.getresponse()
        # but we won't so the connection is dirty
        pooled.release()

        poolset = pool._set
        self.assertEqual(len(poolset), 0)

    def test_context_manager_exception_safety(self):
        class TestError(Exception):
            pass

        for i in xrange(10):
            pool = None
            try:
                with PooledHTTPConnection(
                        self.netloc, self.scheme,
                        size=1, attach_context=True) as conn:
                    pool = conn._pool_context._pool
                    raise TestError()
            except TestError:
                self.assertTrue(pool is not None)
                self.assertEqual(pool._semaphore._Semaphore__value, 1)


class ProcessSafetyTestCase(unittest.TestCase):

    pool_class = NumbersPool

    def setUp(self):
        size = 3000
        self.size = size
        self.pool = self.pool_class(size)
        self.exit_at_tear_down = 0

    def tearDown(self):
        if self.exit_at_tear_down:
            from signal import SIGKILL
            from os import getpid, kill
            kill(getpid(), SIGKILL)

    def test_fork(self):
        from os import fork

        pid = fork()
        if pid == 0:
            self.assertRaises(AssertionError, self.pool.pool_get)
            self.exit_at_tear_down = 1
        else:
            self.pool.pool_get()


if __name__ == '__main__':
    unittest.main()
