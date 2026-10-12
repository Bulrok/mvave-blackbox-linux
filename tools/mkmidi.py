#!/usr/bin/env python3
"""Write a type-0 SMF containing one or more SysEx messages, for aplaymidi."""
import sys

def varlen(n):
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.insert(0, (n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out)

def build(msgs):
    trk = bytearray()
    for m in msgs:
        assert m[0] == 0xF0
        body = m[1:]                      # SMF stores F0 then len then rest incl. F7
        trk += b'\x00\xF0' + varlen(len(body)) + body
    trk += b'\x00\xFF\x2F\x00'
    return (b'MThd' + (6).to_bytes(4,'big') + (0).to_bytes(2,'big') +
            (1).to_bytes(2,'big') + (96).to_bytes(2,'big') +
            b'MTrk' + len(trk).to_bytes(4,'big') + bytes(trk))

if __name__ == '__main__':
    out = sys.argv[1]
    hexstr = ' '.join(sys.argv[2:]).replace(',', ' ')
    msgs, cur = [], bytearray()
    for tok in hexstr.split():
        b = int(tok, 16)
        cur.append(b)
        if b == 0xF7:
            msgs.append(bytes(cur)); cur = bytearray()
    if cur: msgs.append(bytes(cur))
    open(out, 'wb').write(build(msgs))
    print(f"wrote {out}: {len(msgs)} sysex msg(s)")
