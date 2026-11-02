import asyncio,base64,json,re,struct,unittest
from pathlib import Path
from conpty import Terminal
ROOT=Path(__file__).resolve().parents[2]
PATTERN=re.compile(rb'\x1b\]482202;2;GBW1:([A-Za-z0-9+/=]+)(?:\x07|\x1b\\)')
def encode(kind,ident=0,payload=b''):
    raw=struct.pack('!2sBBII',b'GB',5,kind,ident,len(payload))+payload
    raw=raw.replace(b'\x1d',b'\x1d\x3d').replace(b'\x1b',b'\x1d\x3b')
    return base64.b64encode(raw)+b'\n'
class Frames:
    def __init__(self,terminal):self.t=terminal;self.pos=0;self.osc=b'';self.buffer=b'';self.escape=False;self.events=[]
    def poll(self):
        data=bytes(self.t.data);self.osc+=data[self.pos:];self.pos=len(data)
        while match:=PATTERN.search(self.osc):
            raw=base64.b64decode(match[1]);self.osc=self.osc[match.end():]
            for b in raw:
                if self.escape:self.buffer+=bytes([b^32]);self.escape=False
                elif b==29:self.escape=True
                else:self.buffer+=bytes([b])
            while len(self.buffer)>=12:
                magic,version,kind,ident,n=struct.unpack('!2sBBII',self.buffer[:12])
                assert magic==b'GB' and version==5 and n<=65536
                if len(self.buffer)<12+n:break
                self.events.append((kind,ident,self.buffer[12:12+n]));self.buffer=self.buffer[12+n:]
        if len(self.osc)>4096:self.osc=self.osc[-512:]
    async def next(self):
        async def wait():
            while True:
                self.poll()
                if self.events:return self.events.pop(0)
                await asyncio.sleep(.005)
        return await asyncio.wait_for(wait(),15)
class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_windows_console_binary_echo_and_half_close(self):
        async def echo(reader,writer):
            data=await reader.read();writer.write(data);await writer.drain();writer.close();await writer.wait_closed()
        target=await asyncio.start_server(echo,'127.0.0.1',0)
        t=Terminal([str(ROOT/'target/release/guac-bridge-agent.exe'),'--stdio','--windows-console','--target-port',str(target.sockets[0].getsockname()[1])]);f=Frames(t)
        try:
            ready=await f.next();self.assertEqual(ready[:2],(1,0));self.assertEqual(json.loads(ready[2])['version'],5)
            t.write(encode(2,1));self.assertEqual((await f.next())[:2],(3,1))
            payload=bytes(range(256))*256+b'\x1b]482203;\x07'
            for offset in range(0,len(payload),16384):t.write(encode(4,1,payload[offset:offset+16384]))
            t.write(encode(6,1));received=bytearray();eof=False
            while True:
                kind,ident,body=await f.next()
                if kind==4:received.extend(body);t.write(encode(5,ident))
                elif kind==6:eof=True
                elif kind==8:break
                elif kind==9:self.fail(body.decode())
            self.assertTrue(eof);self.assertEqual(bytes(received),payload)
            t.write(encode(11));self.assertEqual(await asyncio.to_thread(t.wait),0)
        finally:
            t.close();target.close();await target.wait_closed()
