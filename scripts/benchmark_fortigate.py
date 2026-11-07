"""Benchmark the FortiGate printable carrier using the local simulated gateway.

This requires Linux and the test dependencies; it does not contact a real portal.
The Python simulator shares the benchmark process and can limit measured speed.
"""
import argparse, asyncio, hashlib, json, os, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tests/native'))
from test_fortigate import FortigateTests

def cpu(pid):
    try:
        fields=open(f'/proc/{pid}/stat').read().split(') ',1)[1].split()
        return (int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK')
    except FileNotFoundError:return 0

async def trial(direction,window,mib,rtt_ms):
    f=FortigateTests('test_binary_transfer_concurrency_and_half_close')
    await f.asyncSetUp()
    try:
        payload=bytes(range(256))*4096
        total=mib*len(payload)
        received=asyncio.Queue()
        async def target(r,w):
            digest=hashlib.sha256()
            try:
                if direction=='download':
                    for _ in range(mib):w.write(payload);await w.drain()
                    w.write_eof();await w.drain()
                else:
                    while data:=await r.read(65536):digest.update(data)
                    await received.put(digest.digest())
                    w.write(b'OK');await w.drain()
            finally:w.close()
        server=await asyncio.start_server(target,'127.0.0.1',0)
        f.servers.append(server);f.target_port=server.sockets[0].getsockname()[1]
        original=f.args
        f.args=lambda *a,**k:original(*a,**k)+['--window',str(window)]
        child,port=await f.launch()
        helper=f.helpers[-1]
        delay_tasks=[]
        if rtt_ms:
            delay=rtt_ms/2000
            async def delayed_worker(queue,send):
                while True:
                    due,data=await queue.get()
                    await asyncio.sleep(max(0,due-asyncio.get_running_loop().time()))
                    await send(data)
            websocket=f.sockets[-1]
            original_send=websocket.send_str
            outbox=asyncio.Queue(maxsize=1024)
            async def delayed_send(data,*a,**k):
                await outbox.put((asyncio.get_running_loop().time()+delay,data))
            websocket.send_str=delayed_send
            delay_tasks.append(asyncio.create_task(delayed_worker(outbox,original_send)))
            original_stdin=helper.stdin
            inbox=asyncio.Queue(maxsize=1024)
            async def deliver(data):
                original_stdin.write(data)
                await original_stdin.drain()
            class DelayedInput:
                def write(self,data):inbox.put_nowait((asyncio.get_running_loop().time()+delay,data))
                async def drain(self):pass
                def __getattr__(self,name):return getattr(original_stdin,name)
            helper.stdin=DelayedInput()
            delay_tasks.append(asyncio.create_task(delayed_worker(inbox,deliver)))
        c0,h0,p0=cpu(child.pid),cpu(helper.pid),time.process_time()
        start=time.perf_counter()
        r,w=await asyncio.open_connection('127.0.0.1',port)
        digest=hashlib.sha256()
        if direction=='download':
            n=0
            while data:=await r.read(65536):digest.update(data);n+=len(data)
            assert n==total
        else:
            for _ in range(mib):w.write(payload);digest.update(payload);await w.drain()
            w.write_eof();assert await r.read()==b'OK'
            assert await received.get()==digest.digest()
        expected=hashlib.sha256()
        for _ in range(mib):expected.update(payload)
        assert digest.digest()==expected.digest()
        elapsed=time.perf_counter()-start
        print(json.dumps(dict(direction=direction,window=window,rtt_ms=rtt_ms,mib_s=mib/elapsed,wall_s=elapsed,client_cpu_s=cpu(child.pid)-c0,helper_cpu_s=cpu(helper.pid)-h0,gateway_cpu_s=time.process_time()-p0)),flush=True)
        w.close();await w.wait_closed()
    finally:
        for task in locals().get('delay_tasks',[]):task.cancel()
        await asyncio.gather(*locals().get('delay_tasks',[]),return_exceptions=True)
        await f.asyncTearDown()

async def main(args):
    for direction in args.direction:
        for window in args.window:
            for _ in range(args.repeats):
                await trial(direction,window,args.mib,args.rtt_ms)

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mib',type=int,default=16)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--rtt-ms',type=float,default=0)
    parser.add_argument('--window',type=int,nargs='+',default=[4,16],choices=range(1,65))
    parser.add_argument('--direction',nargs='+',choices=['download','upload'],default=['download','upload'])
    args=parser.parse_args()
    if sys.platform!='linux':parser.error('this simulator benchmark requires Linux and /proc')
    if args.rtt_ms<0:parser.error('RTT must be nonnegative')
    if min(args.mib,args.repeats)<1:parser.error('sizes and repeats must be positive')
    asyncio.run(main(args))
