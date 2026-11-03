"""Measure fresh helper uploads through a real Bash PTY and simulated remote ACK latency.
No GitHub or external server is contacted. Gateway input ACKs are not delayed.
"""
import argparse, asyncio, base64, hashlib, json, math, os, statistics, sys, tempfile, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests/native'))
from gateway import Gateway
BINARY=ROOT/'target/release/guac-bridge'
AGENT=ROOT/'crates/guac-bridge/assets/guac-bridge-agent-linux-x86_64'
async def run(batch, delay, drop=False):
    with tempfile.TemporaryDirectory(prefix='guac-upload-batch-') as tmp:
        home=Path(tmp);gateway=Gateway(home)
        gateway.upload_ack_delay=delay
        if drop:gateway.drop_upload_after=12000
        url=await gateway.start();child=None
        try:
            start=time.monotonic()
            child=await asyncio.create_subprocess_exec(str(BINARY),'ssh','--guacamole-url',url,'--guacamole-user','test','--no-keyring','--local-port','0','--startup-delay','0','--benchmark-control','--upload-batch-size',str(batch),'--connect-timeout',str(1 if drop else 10),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=dict(os.environ,GUAC_BRIDGE_PASSWORD='test',NO_PROXY='127.0.0.1,localhost'))
            line=await asyncio.wait_for(child.stdout.readline(),90)
            wall=time.monotonic()-start
            if drop:
                await asyncio.wait_for(child.wait(),5)
                error=(await child.stderr.read()).decode()
                assert child.returncode!=0 and not line,error
                assert 'Remote shell did not acknowledge helper upload block' in error,error
                assert gateway.terminals[0].dropped_upload_bytes>0
                return {'batch_bytes':batch,'drop_detected':True,'seconds':round(wall,3)}
            assert line,(await child.stderr.read()).decode()
            json.loads(line)
            asset=AGENT.read_bytes();digest=hashlib.sha256(asset).hexdigest()
            installed=home/'cache/guac-bridge/helpers'/digest
            assert installed.read_bytes()==asset
            expected=math.ceil(len(base64.b64encode(asset))/batch)
            assert gateway.upload_ack_count==expected,(gateway.upload_ack_count,expected)
            child.stdin.write(b'quit\n');await child.stdin.drain();await asyncio.wait_for(child.wait(),10)
            assert child.returncode==0,(await child.stderr.read()).decode()
            return {'batch_bytes':batch,'seconds':round(wall,3),'remote_acks':expected,'integrity':True}
        finally:
            if child and child.returncode is None:child.kill();await child.wait()
            await gateway.close()
async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delay-ms',type=float,default=20)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--output',type=Path,default=ROOT/'upload-batch-results.json')
    args=parser.parse_args();rows=[];faults=[]
    for batch in (3072,12288,24576,49152):
        runs=[]
        for _ in range(args.repeats):
            row=await run(batch,args.delay_ms/1000);rows.append(row);runs.append(row['seconds'])
            print(json.dumps(row),flush=True)
        print(json.dumps({'batch_bytes':batch,'median_seconds':statistics.median(runs)}),flush=True)
        fault=await run(batch,0,True);faults.append(fault);print(json.dumps(fault),flush=True)
    args.output.write_text(json.dumps({'simulated_remote_ack_delay_ms':args.delay_ms,'helper_bytes':AGENT.stat().st_size,'runs':rows,'faults':faults},indent=2)+'\n')
if __name__=='__main__':asyncio.run(main())
