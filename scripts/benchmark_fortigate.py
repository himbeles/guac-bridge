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

async def trial(direction,window,mib):
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
  print(json.dumps(dict(direction=direction,window=window,mib_s=mib/elapsed,wall_s=elapsed,client_cpu_s=cpu(child.pid)-c0,helper_cpu_s=cpu(helper.pid)-h0,gateway_cpu_s=time.process_time()-p0)),flush=True)
  w.close();await w.wait_closed()
 finally:await f.asyncTearDown()

async def main(args):
 for direction in args.direction:
  for window in args.window:
   for _ in range(args.repeats):
    await trial(direction,window,args.mib)

if __name__ == '__main__':
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--mib',type=int,default=16)
 parser.add_argument('--repeats',type=int,default=3)
 parser.add_argument('--window',type=int,nargs='+',default=[4,16],choices=range(1,65))
 parser.add_argument('--direction',nargs='+',choices=['download','upload'],default=['download','upload'])
 args=parser.parse_args()
 if min(args.mib,args.repeats)<1:parser.error('sizes and repeats must be positive')
 asyncio.run(main(args))
