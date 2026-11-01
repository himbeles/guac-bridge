"""Measure native bridge client CPU and throughput through the real-PTY test gateway."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests/native'))
from gateway import Gateway


async def trial(args):
    with tempfile.TemporaryDirectory() as temp:
        gateway = Gateway(Path(temp))
        url = await gateway.start()
        async def echo(reader,writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
                writer.write_eof()
            finally:
                writer.close()
        server = await asyncio.start_server(echo,'127.0.0.1',0)
        env = dict(os.environ,GUAC_BRIDGE_PASSWORD='test',NO_PROXY='127.0.0.1')
        child = await asyncio.create_subprocess_exec(str(args.client.resolve()),'ssh','--url',url,'--username','test','--no-keyring','--port','0','--target-port',str(server.sockets[0].getsockname()[1]),'--startup-delay','0','--window',str(args.window),'--frame-size',str(args.frame_size),'--benchmark-control',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env)
        async def command(text):
            child.stdin.write(text.encode()+b'\n')
            await child.stdin.drain()
            line = await asyncio.wait_for(child.stdout.readline(),120)
            if not line:
                raise RuntimeError((await child.stderr.read()).decode())
            return json.loads(line)
        try:
            endpoint = json.loads(await asyncio.wait_for(child.stdout.readline(),120))['endpoint']
            payload = bytes(range(256))*4096
            repeats = args.mib
            results = []
            for _ in range(args.repeats):
                await command('start')
                start = time.perf_counter()
                reader,writer = await asyncio.open_connection('127.0.0.1',endpoint)
                sent = hashlib.sha256()
                received = hashlib.sha256()
                async def upload():
                    for _ in range(repeats):
                        writer.write(payload)
                        sent.update(payload)
                        await writer.drain()
                    writer.write_eof()
                async def download():
                    total = 0
                    while data := await reader.read(65536):
                        received.update(data)
                        total += len(data)
                    return total
                _,total = await asyncio.wait_for(asyncio.gather(upload(),download()),120)
                writer.close()
                await writer.wait_closed()
                elapsed = time.perf_counter()-start
                counters = await command('stop')
                assert sent.digest()==received.digest() and total==args.mib*1048576
                results.append({**counters,'bytes_each_direction':total,'mib_per_s_each_direction':args.mib/elapsed,'window':args.window,'frame_size':args.frame_size})
            child.stdin.write(b'quit\n')
            await child.stdin.drain()
            await asyncio.wait_for(child.wait(),10)
            assert child.returncode==0,(await child.stderr.read()).decode()
            return results
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
            server.close()
            await server.wait_closed()
            await gateway.close()

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--client',type=Path,default=ROOT/'target/release/guac-bridge')
p.add_argument('--mib',type=int,default=32)
p.add_argument('--repeats',type=int,default=3)
p.add_argument('--window',type=int,choices=(16,32,64),default=32)
p.add_argument('--frame-size',type=int,choices=(16384,32768,65536),default=65536)
p.add_argument('--output',type=Path)
a = p.parse_args()
if a.mib<1 or a.repeats<1:
    p.error('mib and repeats must be positive')
result = {'client_sha256':hashlib.sha256(a.client.read_bytes()).hexdigest(),'trials':asyncio.run(trial(a))}
text = json.dumps(result,indent=2)
print(text)
if a.output:
    a.output.write_text(text+'\n')
