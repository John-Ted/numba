'''
This file implements the code-generator for parallel-vectorize.

ParallelUFunc is the platform independent base class for generating
the thread dispatcher.  This thread dispatcher launches threads
that execute the generated function of UFuncCore.
UFuncCore is subclassed to specialize for the input/output types.
The actual workload is invoked inside the function generated by UFuncCore.
UFuncCore also defines a work-stealing mechanism that allows idle threads
to steal works from other threads.
'''

from llvm.core import *
from llvm.passes import *
from llvm.ee import TargetMachine

from llvm_cbuilder import *
import llvm_cbuilder.shortnames as C

import numpy as np

import sys


from . import _common

ENABLE_WORK_STEALING = True
CHECK_RACE_CONDITION = True

# Granularity controls how many works are removed by the workqueue each time.
# Applies to normal and stealing mode.
# Too small and there will be too many cache synchronization.
GRANULARITY = 256

class WorkQueue(CStruct):
    '''structure for workqueue for parallel-ufunc.
    '''

    _fields_ = [
        ('next', C.intp),  # next index of work item
        ('last', C.intp),  # last index of work item (exlusive)
        ('lock', C.int),   # for locking the workqueue
    ]


    def Lock(self):
        '''inline the lock procedure.
        '''
        if ENABLE_WORK_STEALING:
            with self.parent.loop() as loop:
                with loop.condition() as setcond:
                    unlocked = self.parent.constant(self.lock.type, 0)
                    locked = self.parent.constant(self.lock.type, 1)

                    res = self.lock.reference().atomic_cmpxchg(unlocked, locked,
                                                   ordering='acquire')  #acquire
                    setcond( res != unlocked )

                with loop.body():
                    pass

    def Unlock(self):
        '''inline the unlock procedure.
        '''
        if ENABLE_WORK_STEALING:
            unlocked = self.parent.constant(self.lock.type, 0)
            locked = self.parent.constant(self.lock.type, 1)

            res = self.lock.reference().atomic_cmpxchg(locked, unlocked,
                                                       ordering='release')

            with self.parent.ifelse( res != locked ) as ifelse:
                with ifelse.then():
                    # This shall kill the program
                    self.parent.unreachable()


class ContextCommon(CStruct):
    '''structure for thread-shared context information in parallel-ufunc.
    '''
    _fields_ = [
        # loop ufunc args
        ('args',        C.pointer(C.char_p)),
        ('dimensions',  C.pointer(C.intp)),
        ('steps',       C.pointer(C.intp)),
        ('data',        C.void_p),
        # specifics for work queues
        #('func',        C.void_p),
        ('num_thread',  C.int),
        ('workqueues',  C.pointer(WorkQueue.llvm_type())),
    ]

class Context(CStruct):
    '''structure for thread-specific context information in parallel-ufunc.
    '''
    _fields_ = [
        ('common',    C.pointer(ContextCommon.llvm_type())),
        ('id',        C.int),
        ('completed', C.intp),
    ]

class ParallelUFunc(CDefinition):
    '''the generic parallel vectorize mechanism

    Can be specialized to the maximum number of threads on the platform.


    Platform dependent threading function is implemented in

    def _dispatch_worker(self, worker, contexts, num_thread):
        ...

    which should be implemented in subclass or mixin.
    '''

    _argtys_ = [
        ('worker',     C.void_p, [ATTR_NO_ALIAS]),
        ('args',       C.pointer(C.char_p), [ATTR_NO_ALIAS]),
        ('dimensions', C.pointer(C.intp), [ATTR_NO_ALIAS]),
        ('steps',      C.pointer(C.intp), [ATTR_NO_ALIAS]),
        ('data',       C.void_p, [ATTR_NO_ALIAS]),
    ]

    @classmethod
    def specialize(cls, num_thread):
        '''specialize to the maximum # of thread
        '''
        cls._name_ = 'parallel_ufunc_%d' % num_thread
        cls.ThreadCount = num_thread

    def body(self, worker, args, dimensions, steps, data):

        # Determine chunksize, initial count of work-items per thread.
        # If total_work >= num_thread, equally divide the works.
        # If total_work % num_thread != 0, the last thread does all remaining works.
        # If total_work < num_thread, each thread does one work,
        # and set num_thread to total_work

        num_thread = self.var(C.int, self.ThreadCount, name='num_thread')

        N = dimensions[0]
        ChunkSize = self.var_copy(N / num_thread.cast(N.type))
        ChunkSize_NULL = self.constant_null(ChunkSize.type)
        with self.ifelse(ChunkSize == ChunkSize_NULL) as ifelse:
            with ifelse.then():
                ChunkSize.assign(self.constant(ChunkSize.type, 1))
                num_thread.assign(N.cast(num_thread.type))

        # Setup variables
        common = self.var(ContextCommon, name='common')
        workqueues = self.array(WorkQueue, num_thread, name='workqueues')
        contexts = self.array(Context, num_thread, name='contexts')

        # Initialize ContextCommon
        common.args.assign(args)
        common.dimensions.assign(dimensions)
        common.steps.assign(steps)
        common.data.assign(data)
        common.workqueues.assign(workqueues.reference())
        common.num_thread.assign(num_thread.cast(C.int))


        # Populate workqueue for all threads
        self._populate_workqueues(workqueues, N, ChunkSize, num_thread)

        # Populate contexts for all threads
        self._populate_context(contexts, common, num_thread)

        # Dispatch worker threads
        self._dispatch_worker(worker, contexts,  num_thread)

        ## DEBUG ONLY ##
        # Check for race condition
        if CHECK_RACE_CONDITION:
            total_completed = self.var(C.intp, 0, name='total_completed')
            with self.for_range(num_thread) as (forloop, t):
                cur_ctxt = contexts[t].as_struct(Context)
                total_completed += cur_ctxt.completed
                # self.debug(cur_ctxt.id, 'completed', cur_ctxt.completed)

            with self.ifelse( total_completed == N ) as ifelse:
                with ifelse.then():
                    # self.debug("All is well!")
                    pass # keep quite if all is well
                with ifelse.otherwise():
                    self.debug("ERROR: race occurred! Trigger segfault")
                    self.debug('completed ', total_completed, '/', N)
                    self.unreachable()

        # Return
        self.ret()

    def _populate_workqueues(self, workqueues, N, ChunkSize, num_thread):
        '''loop over all threads and populate the workqueue for each of them.
        '''
        ONE = self.constant(num_thread.type, 1)
        with self.for_range(num_thread) as (loop, i):
            cur_wq = workqueues[i].as_struct(WorkQueue)
            cur_wq.next.assign(i.cast(ChunkSize.type) * ChunkSize)
            cur_wq.last.assign((i + ONE).cast(ChunkSize.type) * ChunkSize)
            cur_wq.lock.assign(self.constant(C.int, 0))
        # end loop
        last_wq = workqueues[num_thread - ONE].as_struct(WorkQueue)
        last_wq.last.assign(N)

    def _populate_context(self, contexts, common, num_thread):
        '''loop over all threads and populate contexts for each of them.
        '''
        ONE = self.constant(num_thread.type, 1)
        with self.for_range(num_thread) as (loop, i):
            cur_ctxt = contexts[i].as_struct(Context)
            cur_ctxt.common.assign(common.reference())
            cur_ctxt.id.assign(i)
            cur_ctxt.completed.assign(
                                    self.constant_null(cur_ctxt.completed.type))

class ParallelUFuncPosixMixin(object):
    '''ParallelUFunc mixin that implements _dispatch_worker to use pthread.
    '''
    def _dispatch_worker(self, worker, contexts, num_thread):
        api = PThreadAPI(self)
        NULL = self.constant_null(C.void_p)

        threads = self.array(api.pthread_t, num_thread, name='threads')

        # self.debug("launch threads")

        with self.for_range(num_thread) as (loop, i):
            status = api.pthread_create(threads[i].reference(), NULL, worker,
                                        contexts[i].reference().cast(C.void_p))
            with self.ifelse(status != self.constant_null(status.type)) as ifelse:
                with ifelse.then():
                    self.debug("Error at pthread_create: ", status)
                    self.unreachable()

        with self.for_range(num_thread) as (loop, i):
            status = api.pthread_join(threads[i], NULL)
            with self.ifelse(status != self.constant_null(status.type)) as ifelse:
                with ifelse.then():
                    self.debug("Error at pthread_join: ", status)
                    self.unreachable()

class ParallelUFuncWindowsMixin(object):
    '''ParallelUFunc mixin that implements _dispatch_worker to use Windows threading.
    '''
    def _dispatch_worker(self, worker, contexts, num_thread):
        api = WinThreadAPI(self)
        NULL = self.constant_null(C.void_p)
        lpdword_NULL = self.constant_null(C.pointer(C.int32))
        zero = self.constant(C.int32, 0)
        intp_zero = self.constant(C.intp, 0)
        INFINITE = self.constant(C.int32, 0xFFFFFFFF)

        threads = self.array(api.handle_t, num_thread, name='threads')

        # self.debug("launch threads")
        # TODO error handling

        with self.for_range(num_thread) as (loop, i):
            threads[i] = api.CreateThread(NULL, intp_zero, worker,
                               contexts[i].reference().cast(C.void_p),
                               zero, lpdword_NULL)

        with self.for_range(num_thread) as (loop, i):
            api.WaitForSingleObject(threads[i], INFINITE)
            api.CloseHandle(threads[i])

class UFuncCore(CDefinition):
    '''core work of a ufunc worker thread

    Subclass to implement UFuncCore._do_work

    Generates the workqueue handling and work stealing and invoke
    the work function for each work item.
    '''
    _name_ = 'ufunc_worker'
    _argtys_ = [
        ('context', C.pointer(Context.llvm_type()), [ATTR_NO_ALIAS]),
        ]

    def body(self, context):
        context = context.as_struct(Context)
        common = context.common.as_struct(ContextCommon)
        tid = context.id

        # self.debug("start thread", tid, "/", common.num_thread)
        workqueue = common.workqueues[tid].as_struct(WorkQueue)

        self._do_workqueue(common, workqueue, tid, context.completed)
        if ENABLE_WORK_STEALING:
            self._do_work_stealing(common, tid, context.completed) # optional

        self.ret()

    def _do_workqueue(self, common, workqueue, tid, completed):
        '''process local workqueue.
        '''
        ZERO = self.constant_null(C.int)


        with self.forever() as loop:
            workqueue.Lock()
            # Critical section
            item = self.var_copy(workqueue.next, name='item')
            AMT = self.min(self.constant(item.type, GRANULARITY),
                           workqueue.last - workqueue.next)

            workqueue.next += AMT
            last = self.var_copy(workqueue.last, name='last')
            # Release
            workqueue.Unlock()

            with self.ifelse( item >= last ) as ifelse:
                with ifelse.then():
                    loop.break_loop()


            #with self.for_range(AMT) as (loop, offset):
            self._do_work(common, item, AMT, tid)
            completed += AMT

    def _do_work_stealing(self, common, tid, completed):
        '''steal work from other workqueues.
        '''
        # self.debug("start work stealing", tid)
        steal_continue = self.var(C.int, 1)
        STEAL_STOP = self.constant_null(steal_continue.type)

        # Loop until all workqueues are done.
        with self.loop() as loop:
            with loop.condition() as setcond:
                setcond( steal_continue != STEAL_STOP )

            with loop.body():
                steal_continue.assign(STEAL_STOP)
                self._do_work_stealing_innerloop(common, steal_continue, tid,
                                                 completed)

    def _do_work_stealing_innerloop(self, common, steal_continue, tid,
                                    completed):
        '''loop over all other threads and try to steal work.
        '''
        with self.for_range(common.num_thread) as (loop, i):
            with self.ifelse( i != tid ) as ifelse:
                with ifelse.then():
                    otherqueue = common.workqueues[i].as_struct(WorkQueue)
                    self._do_work_stealing_check(common, otherqueue,
                                                 steal_continue, tid,
                                                 completed)

    def _do_work_stealing_check(self, common, otherqueue, steal_continue, tid,
                                completed):
        '''check the workqueue for any remaining work and steal it.
        '''
        otherqueue.Lock()
        # Acquired
        STEAL_AMT = self.constant(otherqueue.last.type, GRANULARITY)
        STEAL_CONTINUE = self.constant(steal_continue.type, 1)
        with self.ifelse(otherqueue.next <= otherqueue.last - STEAL_AMT) as ifelse:
            with ifelse.then():
                otherqueue.last -= STEAL_AMT
                item = self.var_copy(otherqueue.last)

                otherqueue.Unlock()
                # Released

                #with self.for_range(STEAL_AMT) as (loop, offset):
                self._do_work(common, item, STEAL_AMT, tid)
                completed += STEAL_AMT

                # Mark incomplete thread
                steal_continue.assign(STEAL_CONTINUE)

            with ifelse.otherwise():
                otherqueue.Unlock()
                # Released

    def _do_work(self, common, item, count, tid):
        '''prepare to call the actual work function

        Implementation depends on number and type of arguments.
        '''
        raise NotImplementedError

class SpecializedParallelUFunc(CDefinition):
    '''a generic ufunc that wraps ParallelUFunc, UFuncCore and the workload
    '''
    _argtys_ = [
        ('args',       C.pointer(C.char_p), [ATTR_NO_ALIAS]),
        ('dimensions', C.pointer(C.intp), [ATTR_NO_ALIAS]),
        ('steps',      C.pointer(C.intp), [ATTR_NO_ALIAS]),
        ('data',       C.void_p, [ATTR_NO_ALIAS]),
    ]

    def body(self, args, dimensions, steps, data,):
        pufunc = self.depends(self.PUFuncDef)
        core = self.depends(self.CoreDef)
        to_void_p = lambda x: x.cast(C.void_p)
        pufunc(to_void_p(core), args, dimensions, steps, data,
               inline=True)
        self.ret()

    @classmethod
    def specialize(cls, pufunc_def, core_def):
        '''specialize to a combination of ParallelUFunc, UFuncCore and workload
        '''
        cls._name_ = 'specialized_%s_%s'% (pufunc_def, core_def)
        cls.PUFuncDef = pufunc_def
        cls.CoreDef = core_def

class PThreadAPI(CExternal):
    '''external declaration of pthread API
    '''
    pthread_t = C.void_p

    pthread_create = Type.function(C.int,
                                   [C.pointer(pthread_t),  # thread_t
                                    C.void_p,              # thread attr
                                    C.void_p,              # function
                                    C.void_p])             # arg

    pthread_join = Type.function(C.int, [C.void_p, C.void_p])

class WinThreadAPI(CExternal):
    '''external declaration of pthread API
    '''
    _calling_convention_ = CC_X86_STDCALL

    handle_t = C.void_p

    # lpStartAddress is an LPTHREAD_START_ROUTINE, with the form
    # DWORD ThreadProc (LPVOID lpdwThreadParam )
    CreateThread = Type.function(handle_t,
                                   [C.void_p,            # lpThreadAttributes (NULL for default)
                                    C.intp,              # dwStackSize (0 for default)
                                    C.void_p,            # lpStartAddress
                                    C.void_p,            # lpParameter
                                    C.int32,             # dwCreationFlags (0 for default)
                                    C.pointer(C.int32)]) # lpThreadId (NULL if not required)

    # Return is WAIT_OBJECT_0 (0x00000000) to indicate the thread exited,
    # or WAIT_ABANDONED, WAIT_TIMEOUT, WAIT_FAILED for other conditions.
    WaitForSingleObject = Type.function(C.int32,
                                    [handle_t, # hHandle
                                     C.int32])   # dwMilliseconds (INFINITE == 0xFFFFFFFF means wait forever)

    CloseHandle = Type.function(C.int32, [handle_t])


class UFuncCoreGeneric(UFuncCore):
    '''A generic ufunc core worker from LLVM function type
    '''
    def _do_work(self, common, item, count, tid):
        '''
        common :
        item :
        tid : for debugging
        '''

        ufunc_ptr = CFunc(self, self.WORKER)
        fnty = self.WORKER.type.pointee


        args = common.args
        steps = common.steps

        arg_ptrs = []
        arg_steps = []
        for i in range(len(fnty.args)+1):
            arg_ptrs.append(self.var_copy(args[i][item * steps[i]:]))
            const_step = self.var_copy(steps[i])
            const_step.invariant = True
            arg_steps.append(const_step)

        with self.for_range(count) as (loop, item):
            callargs = []
            for i, argty in enumerate(fnty.args):
                casted = arg_ptrs[i].cast(C.pointer(argty))
                callargs.append(casted.load())
                arg_ptrs[i].assign(arg_ptrs[i][arg_steps[i]:]) # increment pointer

            res = ufunc_ptr(*callargs, **dict(inline=True))
            retval_ptr = arg_ptrs[-1].cast(C.pointer(fnty.return_type))
            retval_ptr.store(res, nontemporal=True)
            arg_ptrs[-1].assign(arg_ptrs[-1][arg_steps[-1]:])

    @classmethod
    def specialize(cls, lfunc):
        '''specialize to a LLVM function type

        fntype : a LLVM function type (llvm.core.FunctionType)
        '''
        fntype = lfunc.type.pointee
        cls._name_ = '.'.join([cls._name_, lfunc.name])

        #cls.RETTY = fntype.return_type
        #cls.ARGTYS = tuple(fntype.args)
        cls.WORKER = lfunc


if sys.platform == 'win32':
    class ParallelUFuncPlatform(ParallelUFunc, ParallelUFuncWindowsMixin):
        pass
else:
    class ParallelUFuncPlatform(ParallelUFunc, ParallelUFuncPosixMixin):
        pass

class _ParallelVectorizeFromFunc(_common.CommonVectorizeFromFrunc):
    def build(self, lfunc):
        import multiprocessing
        NUM_CPU = multiprocessing.cpu_count()
        def_spuf = SpecializedParallelUFunc(
                                    ParallelUFuncPlatform(num_thread=NUM_CPU),
                                    UFuncCoreGeneric(lfunc))

        func = def_spuf(lfunc.module)

        _common.post_vectorize_optimize(func)

        return func

parallel_vectorize_from_func = _ParallelVectorizeFromFunc()

class ParallelVectorize(_common.GenericVectorize):
    def build_ufunc(self):
        assert self.translates, "No translation"
        lfunclist = self._get_lfunc_list()
        tyslist = self._get_tys_list()
        engine = self.translates[0]._get_ee()
        return parallel_vectorize_from_func(lfunclist, tyslist, engine=engine)

