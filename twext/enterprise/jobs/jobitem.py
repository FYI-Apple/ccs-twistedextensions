# -*- test-case-name: twext.enterprise.test.test_queue -*-
##
# Copyright (c) 2012-2015 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##

from twext.enterprise.dal.model import Sequence
from twext.enterprise.dal.model import Table, Schema, SQLType
from twext.enterprise.dal.record import Record, fromTable, NoSuchRecord
from twext.enterprise.dal.syntax import SchemaSyntax
from twext.enterprise.ienterprise import ORACLE_DIALECT
from twext.enterprise.jobs.utils import inTransaction, astimestamp
from twext.python.log import Logger

from twisted.internet.defer import inlineCallbacks, returnValue, Deferred
from twisted.protocols.amp import Argument
from twisted.python.failure import Failure

from datetime import datetime, timedelta
from collections import namedtuple
import time

log = Logger()

"""
A job is split into two pieces: an L{JobItem} (defined in this module) and an
L{WorkItem} (defined in twext.enterprise.jobs.workitem). Each type of work has
its own L{WorkItem} subclass. The overall work queue is a single table of
L{JobItem}s which reference all the various L{WorkItem} tables. The
L{ControllerQueue} then processes the items in the L{JobItem} table, which
result in the appropriate L{WotkItem} being run. This split allows a single
processing queue to handle many different types of work, each of which may have
its own set of parameters.
"""

def makeJobSchema(inSchema):
    """
    Create a self-contained schema for L{JobInfo} to use, in C{inSchema}.

    @param inSchema: a L{Schema} to add the job table to.
    @type inSchema: L{Schema}

    @return: a schema with just the one table.
    """
    # Initializing this duplicate schema avoids a circular dependency, but this
    # should really be accomplished with independent schema objects that the
    # transaction is made aware of somehow.
    JobTable = Table(inSchema, "JOB")

    JobTable.addColumn("JOB_ID", SQLType("integer", None), default=Sequence(inSchema, "JOB_SEQ"), notNull=True, primaryKey=True)
    JobTable.addColumn("WORK_TYPE", SQLType("varchar", 255), notNull=True)
    JobTable.addColumn("PRIORITY", SQLType("integer", 0), default=0)
    JobTable.addColumn("WEIGHT", SQLType("integer", 0), default=0)
    JobTable.addColumn("NOT_BEFORE", SQLType("timestamp", None), notNull=True)
    JobTable.addColumn("ASSIGNED", SQLType("timestamp", None), default=None)
    JobTable.addColumn("OVERDUE", SQLType("timestamp", None), default=None)
    JobTable.addColumn("FAILED", SQLType("integer", 0), default=0)
    JobTable.addColumn("PAUSE", SQLType("integer", 0), default=0)

    return inSchema

JobInfoSchema = SchemaSyntax(makeJobSchema(Schema(__file__)))



class JobFailedError(Exception):
    """
    A job failed to run - we need to be smart about clean up.
    """

    def __init__(self, ex):
        self._ex = ex



class JobTemporaryError(Exception):
    """
    A job failed to run due to a temporary failure. We will get the job to run again after the specified
    interval (with a built-in back-off based on the number of failures also applied).
    """

    def __init__(self, delay):
        """
        @param delay: amount of time in seconds before it should run again
        @type delay: L{int}
        """
        self.delay = delay



class JobRunningError(Exception):
    """
    A job is already running.
    """
    pass



class JobItem(Record, fromTable(JobInfoSchema.JOB)):
    """
    @DynamicAttrs
    An item in the job table. This is typically not directly used by code
    creating work items, but rather is used for internal book keeping of jobs
    associated with work items.

    The JOB table has some important columns that determine how a job is being scheduled:

    NOT_BEFORE - this is a timestamp indicating when the job is expected to run. It will not
    run before this time, but may run quite some time after (if the service is busy).

    ASSIGNED - this is a timestamp that is initially NULL but set when the job processing loop
    assigns the job to a child process to be executed. Thus, if the value is not NULL, then the
    job is (probably) being executed. The child process is supposed to delete the L{JobItem}
    when it is done, however if the child dies without executing the job, then the job
    processing loop needs to detect it.

    OVERDUE - this is a timestamp initially set when an L{JobItem} is assigned. It represents
    a point in the future when the job is expected to be finished. The job processing loop skips
    jobs that have a non-NULL ASSIGNED value and whose OVERDUE value has not been passed. If
    OVERDUE is in the past, then the job processing loop checks to see if the job is still
    running - which is determined by whether a row lock exists on the work item (see
    L{isRunning}. If the job is still running then OVERDUE is bumped up to a new point in the
    future, if it is not still running the job is marked as failed - which will reschedule it.

    FAILED - a count of the number of times a job has failed or had its overdue count bumped.

    The above behavior depends on some important locking behavior: when an L{JobItem} is run,
    it locks the L{WorkItem} row corresponding to the job (it may lock other associated
    rows - e.g., other L{WorkItem}'s in the same group). It does not lock the L{JobItem}
    row corresponding to the job because the job processing loop may need to update the
    OVERDUE value of that row if the work takes a long time to complete.
    """

    _workTypes = None
    _workTypeMap = None

    lockRescheduleInterval = 60     # When a job can't run because of a lock, reschedule it this number of seconds in the future
    failureRescheduleInterval = 60  # When a job fails, reschedule it this number of seconds in the future

    def descriptor(self):
        return JobDescriptor(self.jobID, self.weight, self.workType)


    def assign(self, when, overdue):
        """
        Mark this job as assigned to a worker by setting the assigned column to the current,
        or provided, timestamp. Also set the overdue value to help determine if a job is orphaned.

        @param when: current timestamp
        @type when: L{datetime.datetime}
        @param overdue: number of seconds after assignment that the job will be considered overdue
        @type overdue: L{int}
        """
        return self.update(assigned=when, overdue=when + timedelta(seconds=overdue))


    def bumpOverdue(self, bump):
        """
        Increment the overdue value by the specified number of seconds. Used when an overdue job
        is still running in a child process but the job processing loop has detected it as overdue.

        @param bump: number of seconds to increment overdue by
        @type bump: L{int}
        """
        return self.update(overdue=self.overdue + timedelta(seconds=bump))


    def failedToRun(self, locked=False, delay=None):
        """
        The attempt to run the job failed. Leave it in the queue, but mark it
        as unassigned, bump the failure count and set to run at some point in
        the future.

        @param lock: indicates if the failure was due to a lock timeout.
        @type lock: L{bool}
        @param delay: how long before the job is run again, or C{None} for a default
            staggered delay behavior.
        @type delay: L{int}
        """

        # notBefore is set to the chosen interval multiplied by the failure count, which
        # results in an incremental backoff for failures
        if delay is None:
            delay = self.lockRescheduleInterval if locked else self.failureRescheduleInterval
            delay *= (self.failed + 1)
        return self.update(
            assigned=None,
            overdue=None,
            failed=self.failed + (0 if locked else 1),
            notBefore=datetime.utcnow() + timedelta(seconds=delay)
        )


    def pauseIt(self, pause=False):
        """
        Pause the L{JobItem} leaving all other attributes the same. The job processing loop
        will skip paused items.

        @param pause: indicates whether the job should be paused.
        @type pause: L{bool}
        @param delay: how long before the job is run again, or C{None} for a default
            staggered delay behavior.
        @type delay: L{int}
        """

        return self.update(pause=pause)


    @classmethod
    @inlineCallbacks
    def ultimatelyPerform(cls, txnFactory, jobID):
        """
        Eventually, after routing the job to the appropriate place, somebody
        actually has to I{do} it. This method basically calls L{JobItem.run}
        but it does a bunch of "booking" to track the transaction and log failures
        and timing information.

        @param txnFactory: a 0- or 1-argument callable that creates an
            L{IAsyncTransaction}
        @type txnFactory: L{callable}
        @param jobID: the ID of the job to be performed
        @type jobID: L{int}
        @return: a L{Deferred} which fires with C{None} when the job has been
            performed, or fails if the job can't be performed.
        """

        t = time.time()
        def _tm():
            return "{:.3f}".format(1000 * (time.time() - t))
        def _overtm(nb):
            return "{:.0f}".format(1000 * (t - astimestamp(nb)))

        # Failed job clean-up
        def _failureCleanUp(delay=None):
            @inlineCallbacks
            def _cleanUp2(txn2):
                try:
                    job = yield cls.load(txn2, jobID)
                except NoSuchRecord:
                    log.debug(
                        "JobItem: {jobid} disappeared t={tm}",
                        jobid=jobID,
                        tm=_tm(),
                    )
                else:
                    log.debug(
                        "JobItem: {jobid} marking as failed {count} t={tm}",
                        jobid=jobID,
                        count=job.failed + 1,
                        tm=_tm(),
                    )
                    yield job.failedToRun(locked=isinstance(e, JobRunningError), delay=delay)
            return inTransaction(txnFactory, _cleanUp2, "ultimatelyPerform._failureCleanUp")

        log.debug("JobItem: {jobid} starting to run", jobid=jobID)
        txn = txnFactory(label="ultimatelyPerform: {}".format(jobID))
        try:
            job = yield cls.load(txn, jobID)
            if hasattr(txn, "_label"):
                txn._label = "{} <{}>".format(txn._label, job.workType)
            log.debug(
                "JobItem: {jobid} loaded {work} t={tm}",
                jobid=jobID,
                work=job.workType,
                tm=_tm(),
            )
            yield job.run()

        except NoSuchRecord:
            # The record has already been removed
            yield txn.commit()
            log.debug(
                "JobItem: {jobid} already removed t={tm}",
                jobid=jobID,
                tm=_tm(),
            )

        except JobTemporaryError as e:

            # Temporary failure delay with back-off
            def _temporaryFailure():
                return _failureCleanUp(delay=e.delay * (job.failed + 1))
            log.debug(
                "JobItem: {jobid} {desc} {work} t={tm}",
                jobid=jobID,
                desc="temporary failure #{}".format(job.failed + 1),
                work=job.workType,
                tm=_tm(),
            )
            txn.postAbort(_temporaryFailure)
            yield txn.abort()

        except (JobFailedError, JobRunningError) as e:

            # Permanent failure
            log.debug(
                "JobItem: {jobid} {desc} {work} t={tm}",
                jobid=jobID,
                desc="failed" if isinstance(e, JobFailedError) else "locked",
                work=job.workType,
                tm=_tm(),
            )
            txn.postAbort(_failureCleanUp)
            yield txn.abort()

        except:
            f = Failure()
            log.error(
                "JobItem: {jobid} unknown exception t={tm} {exc}",
                jobid=jobID,
                tm=_tm(),
                exc=f,
            )
            yield txn.abort()
            returnValue(f)

        else:
            yield txn.commit()
            log.debug(
                "JobItem: {jobid} completed {work} t={tm} over={over}",
                jobid=jobID,
                work=job.workType,
                tm=_tm(),
                over=_overtm(job.notBefore),
            )

        returnValue(None)


    @classmethod
    @inlineCallbacks
    def nextjob(cls, txn, now, minPriority):
        """
        Find the next available job based on priority, also return any that are overdue. This
        method uses an SQL query to find the matching jobs, and sorts based on the NOT_BEFORE
        value and priority..

        @param txn: the transaction to use
        @type txn: L{IAsyncTransaction}
        @param now: current timestamp - needed for unit tests that might use their
            own clock.
        @type now: L{datetime.datetime}
        @param minPriority: lowest priority level to query for
        @type minPriority: L{int}

        @return: the job record
        @rtype: L{JobItem}
        """

        jobs = yield cls.nextjobs(txn, now, minPriority, limit=1)

        # Must only be one or zero
        if jobs and len(jobs) > 1:
            raise AssertionError("next_job() returned more than one row")

        returnValue(jobs[0] if jobs else None)


    @classmethod
    @inlineCallbacks
    def nextjobs(cls, txn, now, minPriority, limit=1):
        """
        Find the next available job based on priority, also return any that are overdue.

        @param txn: the transaction to use
        @type txn: L{IAsyncTransaction}
        @param now: current timestamp
        @type now: L{datetime.datetime}
        @param minPriority: lowest priority level to query for
        @type minPriority: L{int}
        @param limit: limit on number of jobs to return
        @type limit: L{int}

        @return: the job record
        @rtype: L{JobItem}
        """

        queryExpr = (cls.notBefore <= now).And(cls.priority >= minPriority).And(cls.pause == 0).And(
            (cls.assigned == None).Or(cls.overdue < now)
        )

        if txn.dialect == ORACLE_DIALECT:
            # Oracle does not support a "for update" clause with "order by". So do the
            # "for update" as a second query right after the first. Will need to check
            # how this might impact concurrency in a multi-host setup.
            jobs = yield cls.query(
                txn,
                queryExpr,
                order=(cls.assigned, cls.priority),
                ascending=False,
                limit=limit,
            )
            if jobs:
                yield cls.query(
                    txn,
                    (cls.jobID.In([job.jobID for job in jobs])),
                    forUpdate=True,
                    noWait=False,
                )
        else:
            jobs = yield cls.query(
                txn,
                queryExpr,
                order=(cls.assigned, cls.priority),
                ascending=False,
                forUpdate=True,
                noWait=False,
                limit=limit,
            )

        returnValue(jobs)


    @inlineCallbacks
    def run(self):
        """
        Run this job item by finding the appropriate work item class and
        running that, with appropriate locking.
        """

        workItem = yield self.workItem()
        if workItem is not None:

            # First we lock the L{WorkItem}
            locked = yield workItem.runlock()
            if not locked:
                raise JobRunningError()

            try:
                # Run in three steps, allowing for before/after hooks that sub-classes
                # may override
                okToGo = yield workItem.beforeWork()
                if okToGo:
                    yield workItem.doWork()
                    yield workItem.afterWork()
            except Exception as e:
                f = Failure()
                log.error(
                    "JobItem: {jobid}, WorkItem: {workid} failed: {exc}",
                    jobid=self.jobID,
                    workid=workItem.workID,
                    exc=f,
                )
                if isinstance(e, JobTemporaryError):
                    raise
                else:
                    raise JobFailedError(e)

        try:
            # Once the work is done we delete ourselves - NB this must be the last thing done
            # to ensure the L{JobItem} row is not locked for very long.
            yield self.delete()
        except NoSuchRecord:
            # The record has already been removed
            pass


    @inlineCallbacks
    def isRunning(self):
        """
        Return L{True} if the job is currently running (its L{WorkItem} is locked).
        """
        workItem = yield self.workItem()
        if workItem is not None:
            locked = yield workItem.trylock()
            returnValue(not locked)
        else:
            returnValue(False)


    @inlineCallbacks
    def workItem(self):
        """
        Return the L{WorkItem} corresponding to this L{JobItem}.
        """
        workItemClass = self.workItemForType(self.workType)
        workItems = yield workItemClass.loadForJob(
            self.transaction, self.jobID
        )
        returnValue(workItems[0] if len(workItems) == 1 else None)


    @classmethod
    def workItemForType(cls, workType):
        """
        Return the class of the L{WorkItem} associated with this L{JobItem}.

        @param workType: the name of the L{WorkItem}'s table
        @type workType: L{str}
        """
        if cls._workTypeMap is None:
            cls.workTypes()
        return cls._workTypeMap[workType]


    @classmethod
    def workTypes(cls):
        """
        Map all L{WorkItem} sub-classes table names to the class type.

        @return: All of the work item types.
        @rtype: iterable of L{WorkItem} subclasses
        """
        if cls._workTypes is None:
            cls._workTypes = []
            def getWorkType(subcls, appendTo):
                if hasattr(subcls, "table"):
                    appendTo.append(subcls)
                else:
                    for subsubcls in subcls.__subclasses__():
                        getWorkType(subsubcls, appendTo)
            from twext.enterprise.jobs.workitem import WorkItem
            getWorkType(WorkItem, cls._workTypes)

            cls._workTypeMap = {}
            for subcls in cls._workTypes:
                cls._workTypeMap[subcls.workType()] = subcls

        return cls._workTypes


    @classmethod
    def numberOfWorkTypes(cls):
        return len(cls.workTypes())


    @classmethod
    @inlineCallbacks
    def waitEmpty(cls, txnCreator, reactor, timeout):
        """
        Wait for the job queue to drain. Only use this in tests
        that need to wait for results from jobs.
        """
        t = time.time()
        while True:
            work = yield inTransaction(txnCreator, cls.all)
            if not work:
                break
            if time.time() - t > timeout:
                returnValue(False)
            d = Deferred()
            reactor.callLater(0.1, lambda : d.callback(None))
            yield d

        returnValue(True)


    @classmethod
    @inlineCallbacks
    def waitJobDone(cls, txnCreator, reactor, timeout, jobID):
        """
        Wait for the specified job to complete. Only use this in tests
        that need to wait for results from jobs.
        """
        t = time.time()
        while True:
            work = yield inTransaction(txnCreator, cls.query, expr=(cls.jobID == jobID))
            if not work:
                break
            if time.time() - t > timeout:
                returnValue(False)
            d = Deferred()
            reactor.callLater(0.1, lambda : d.callback(None))
            yield d

        returnValue(True)


    @classmethod
    @inlineCallbacks
    def waitWorkDone(cls, txnCreator, reactor, timeout, workTypes):
        """
        Wait for the specified job to complete. Only use this in tests
        that need to wait for results from jobs.
        """
        t = time.time()
        while True:
            count = [0]

            @inlineCallbacks
            def _countTypes(txn):
                for t in workTypes:
                    work = yield t.all(txn)
                    count[0] += len(work)

            yield inTransaction(txnCreator, _countTypes)
            if count[0] == 0:
                break
            if time.time() - t > timeout:
                returnValue(False)
            d = Deferred()
            reactor.callLater(0.1, lambda : d.callback(None))
            yield d

        returnValue(True)


    @classmethod
    @inlineCallbacks
    def histogram(cls, txn):
        """
        Generate a histogram of work items currently in the queue.
        """
        from twext.enterprise.jobs.queue import WorkerConnectionPool
        results = {}
        now = datetime.utcnow()
        for workItemType in cls.workTypes():
            workType = workItemType.workType()
            results.setdefault(workType, {
                "queued": 0,
                "assigned": 0,
                "late": 0,
                "failed": 0,
                "completed": WorkerConnectionPool.completed.get(workType, 0),
                "time": WorkerConnectionPool.timing.get(workType, 0.0)
            })

        jobs = yield cls.all(txn)

        for job in jobs:
            r = results[job.workType]
            r["queued"] += 1
            if job.assigned is not None:
                r["assigned"] += 1
            if job.assigned is None and job.notBefore < now:
                r["late"] += 1
            if job.failed:
                r["failed"] += 1

        returnValue(results)


JobDescriptor = namedtuple("JobDescriptor", ["jobID", "weight", "type"])

class JobDescriptorArg(Argument):
    """
    Comma-separated representation of an L{JobDescriptor} for AMP-serialization.
    """
    def toString(self, inObject):
        return ",".join(map(str, inObject))


    def fromString(self, inString):
        return JobDescriptor(*[f(s) for f, s in zip((int, int, str,), inString.split(","))])