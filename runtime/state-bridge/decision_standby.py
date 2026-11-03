"""One bounded, read-only Decisions job alongside a slow Sol request."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from time import monotonic


class DecisionStandby:
    def __init__(self, discarded, delay=8):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='decision-standby')
        self.discarded = discarded
        self.delay = delay
        self.active = None

    def reap(self):
        if self.active and self.active['future'].done():
            job,self.active = self.active,None
            result = job['future'].result()
            if result is not None:
                self.discarded(job['origin'],result,job.get('primary_action'))

    def start(self, origin, prepare, *, dispatch_by=None):
        self.reap()
        if self.active:
            return None  # A speculative request from the preceding window is still draining.
        wake, cancel = Event(),Event()
        due = monotonic()+(self.delay if dispatch_by is None else min(self.delay,dispatch_by))
        def work():
            try:
                send = prepare()
                wake.wait(max(0,due-monotonic()))
                if cancel.is_set():
                    return None
                return send()
            except Exception as exc:
                # Backup preparation can fail independently of a valid primary.
                return {'error':'standby unavailable: '+type(exc).__name__,'sent':False}
        self.active = {'origin':origin, 'wake':wake, 'cancel':cancel,
                       'future':self.pool.submit(work)}
        return self.active

    def discard(self, job, primary_action=None):
        if job is not None:
            if primary_action is not None:
                job['primary_action'] = primary_action
            job['cancel'].set()
            job['wake'].set()

    def take_ready(self, job, origin, menu):
        if job is None or self.active is not job or job['origin']!=origin or not job['future'].done():
            return None
        result=job['future'].result()
        if not result or result.get('error') or result.get('action') not in menu.values():
            return None
        self.active=None
        return result

    def take(self, job):
        if job is None:
            return None
        job['wake'].set()  # Also handles an early transport timeout.
        try:
            result = job['future'].result(timeout=6.5)
        except TimeoutError:
            self.discard(job)
            return None
        self.active = None
        return result

    def close(self):
        self.discard(self.active)
        self.pool.shutdown(wait=True)
        self.reap()
