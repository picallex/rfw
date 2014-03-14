from __future__ import print_function
from threading import Thread
import time, logging
import cmdexe, iputil, iptables

log = logging.getLogger('rfw.rfwthreads')


class CommandProcessor(Thread):

    def __init__(self, cmd_queue, whitelist, expiry_queue, default_expire):
        Thread.__init__(self)
        self.cmd_queue = cmd_queue
        self.whitelist = whitelist
        self.expiry_queue = expiry_queue
        self.default_expire = default_expire
        self.setDaemon(True)


    def schedule_expiry(self, rcmd):
        # put time-bounded command to the expiry_queue
        expire = rcmd.get('expire', self.default_expire)
        assert isinstance(expire, str) and expire.isdigit()
        # expire=0 means permanent rule which is not added to the expiry queue
        if expire:
            expiry_tstamp = time.time() + int(expire)
            extup = (expiry_tstamp, rcmd)
            self.expiry_queue.put_nowait(extup)
            log.debug('Expiry queue after put: {}'.format(self.expiry_queue.queue))

    def run(self):
        rules = iptables.Iptables.load().rules
        # get the set of frozen rcmd
        rcmds = cmdexe.rules_to_rcmds(rules)
    
        #TODO make sure if the rcmds format from iptables_list()/rules_to_rcmds() conforms to REST rcmds from cmdparse 
        #TODO add consistency checks
    
        while True:
            modify, rcmd = self.cmd_queue.get()
            try:
                # immutable rcmd dict for rcmds set operations
                frozen_rcmd = frozenset(rcmd.items())
                log.debug("Got new item from the command queue: '{}' {}".format(modify, rcmd))
                rule_exists = frozen_rcmd in rcmds
                log.debug('frozen_rcmd: {}'.format(frozen_rcmd))
                log.debug('rcmds: {}'.format(rcmds))
                log.debug('rule_exists: {}'.format(rule_exists))                

 
                # check for duplicates, apply rule
                if modify == 'I':
                    if rule_exists:
                        log.warn("Trying to insert existing rule: {}. Command ignored.".format(rcmd))
                    else:
                        cmdexe.apply_rule(modify, rcmd)
                        # schedule expiry timeout if present only for Insert rules and only if rule didn't exist before (so it was added now)
                        self.schedule_expiry(rcmd)
                        rcmds.add(frozen_rcmd)
                elif modify == 'D':
                    if rule_exists:
                        #TODO delete rules in the loop to delete actual iptables duplicates. It's to satisfy idempotency and plays well with common sense
                        cmdexe.apply_rule(modify, rcmd)
                        rcmds.discard(frozen_rcmd)
                    else:
                        log.warn("Trying to delete not existing rule: {}. Command ignored.".format(rcmd))
                elif modify == 'L':
                    #TODO rereading the iptables?
                    pass
            finally:    
                self.cmd_queue.task_done()






class ExpiryManager(Thread):
    
    # polling interval in seconds that determines time resolution of expire parameter
    POLL_INTERVAL = 1

    def __init__(self, cmd_queue, expiry_queue):
        """cmd_queue is a FIFO queue of (modify, rcmd) tuples
        expiry_queue is a priority queue of (expiry_tstamp, rcmd) tuples
        """
        Thread.__init__(self)
        self.cmd_queue = cmd_queue
        self.expiry_queue = expiry_queue
        self.setDaemon(True)

    
    def run(self):
        # Not thread safe! It's OK here because we have single producer and single consumer, where consumer need not atomic 'peek and get'
        def peek(q):
            if q.queue:
                return q.queue[0]
            else:
                return None

        while True:
            time.sleep(ExpiryManager.POLL_INTERVAL)

            # Move expired items from expiry_queue to cmd_queue
            item = peek(self.expiry_queue)
            if item is None:
                continue
            expiry_tstamp, rcmd = item
            # skip if the next candidate expires in the future
            if expiry_tstamp > time.time():
                continue
            # get item with lowest priority score. It may be different (but certainly lower) from the one returned by peek() since peek() is not thread safe
            expiry_tstamp, rcmd = self.expiry_queue.get()
            log.debug('Expiry queue after get: {}'.format(self.expiry_queue.queue))
            # expire parameter is valid only for 'I' (insert) commands, so expiring the rule is as simple as deleting it
            tup = ('D', rcmd)
            self.cmd_queue.put_nowait(tup)
            self.expiry_queue.task_done()

            






