# Copyright 2011 Obsidian Research Corp. GPLv2, see COPYING.
import optparse
import pickle
import socket
import contextlib
import sys
import time
from mmap import mmap
from collections import namedtuple
import rdma.ibverbs as ibv;
from rdma.tools import clock_monotonic
import rdma.path
import rdma.vtools
from libibtool import *;
from libibtool.libibopts import *;

infotype = namedtuple('infotype', 'path addr rkey size iters')

class Endpoint(object):
    ctx = None;
    pd = None;
    cq = None;
    mr = None;
    peerinfo = None;

    def __init__(self,opt,dev):
        self.opt = opt
        self.ctx = rdma.get_verbs(dev)
        self.cc = self.ctx.comp_channel();
        self.cq = self.ctx.cq(2*opt.tx_depth,self.cc)
        self.poller = rdma.vtools.CQPoller(self.cq);
        self.pd = self.ctx.pd()
        self.qp = self.pd.qp(ibv.IBV_QPT_RC,
                             opt.tx_depth,
                             self.cq,
                             opt.tx_depth,
                             self.cq,
                             max_send_sge=opt.num_sge,
                             max_recv_sge=1);
        self.mem = mmap(-1, opt.size)
        self.mr = self.pd.mr(self.mem,
                             ibv.IBV_ACCESS_LOCAL_WRITE|ibv.IBV_ACCESS_REMOTE_WRITE)

    def __enter__(self):
        return self;

    def __exit__(self,*exc_info):
        self.close();

    def close(self):
        if self.ctx is not None:
            self.ctx.close();

    def connect(self, peerinfo):
        self.peerinfo = peerinfo
        self.qp.establish(self.path.forward_path,ibv.IBV_ACCESS_REMOTE_WRITE);

    def get_gid(self):
        return self.path.SGID

    def rdma(self):
        if self.opt.num_sge > 1:
            block = self.opt.size / self.opt.num_sge + 1
            sg_list = []
            offset = 0
            while offset < self.opt.size:
                if offset + block > self.opt.size:
                    block = self.opt.size - offset
                sg_list.append( self.mr.sge( block, offset ) )
                offset += block
        else:
            sg_list = self.mr.sge()

        swr = ibv.send_wr(wr_id=0,
                          remote_addr=self.peerinfo[2],
                          sg_list=sg_list,
                          rkey=self.peerinfo[3],
                          opcode=ibv.IBV_WR_RDMA_WRITE,
                          send_flags=ibv.IBV_SEND_SIGNALED)

        n = self.opt.iters
        depth = min(self.opt.tx_depth, n, self.qp.max_send_wr)

        tpost = clock_monotonic()
        for i in xrange(depth):
            self.qp.post_send(swr)

        completions = 0
        posts = depth
        for wc in self.poller.iterwc(timeout=100):
            if wc.status != ibv.IBV_WC_SUCCESS:
                raise ibv.WCError(wc,self.cq,obj=self.qp);
            completions += 1
            if posts < n:
                self.qp.post_send(swr)
                posts += 1
                self.poller.wakeat = rdma.tools.clock_monotonic() + 1;
            if completions == n:
                break;
        else:
            raise rdma.RDMAError("CQ timed out");

        tcomp = clock_monotonic()

        rate = self.opt.size*self.opt.iters/1e6/(tcomp-tpost)
        print "%.1f MB/sec" % rate

def client_mode(hostname,opt,dev):
    with Endpoint(opt,dev) as end:
        ret = socket.getaddrinfo(hostname,str(opt.ip_port),opt.af,
                                 socket.SOCK_STREAM);
        ret = ret[0];
        with contextlib.closing(socket.socket(ret[0],ret[1])) as sock:
            if opt.debug >= 1:
                print "Connecting to %r %r"%(ret[4][0],ret[4][1]);
            sock.connect(ret[4]);

            end = Endpoint(opt, dev)
            end.path = rdma.path.IBPath(dev);
            rdma.path.fill_path(end.qp, end.path, max_rd_atomic = 0)

            end.path.sqpsn = 1
            end.path.dqpsn = 1
            end.path.SGID_index = 0

            end.path.MTU = 3
            end.path.rate = 0
            end.path.has_grh = True

            sgid = end.get_gid()
            sqpn = end.qp.qp_num

            sock.send(pickle.dumps((sgid, sqpn, end.mr.addr, end.mr.rkey)))

            buf = sock.recv(1024)
            peerinfo = pickle.loads(buf)

            end.path.reverse(for_reply=False);
            end.path.forward_path.SGID = sgid
            end.path.forward_path.DGID = IBA.GID(peerinfo[0])
            end.path.forward_path.dqpn = int(peerinfo[1])

            end.connect(peerinfo)
            # Synchronize the transition to RTS
            sock.send("Ready");
            end.rdma()

            sock.shutdown(socket.SHUT_WR);
            sock.recv(1024);

def setup_server_endpoint(port, opt, rdma_end_port=None):
    """Negotiate QP parameters over TCP socket and return Endpoint.
    Listen on the given TCP port."""

    dev = rdma.get_end_port(rdma_end_port)
    ret = socket.getaddrinfo(None,str(port),0,
                             socket.SOCK_STREAM,0,
                             socket.AI_PASSIVE)
    ret = ret[0];
    with contextlib.closing(socket.socket(ret[0],ret[1])) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(ret[4]);
        sock.listen(1)

        print "Listening on port %d for QP setup."%port
        s,addr = sock.accept()

        with contextlib.closing(s):
            (dgid, dqpn, addr, rkey) = pickle.loads(s.recv(1024))
            end = Endpoint(opt, dev)
            end.path = rdma.path.IBPath(dev, DGID = IBA.GID(dgid), dqpn = dqpn );
            rdma.path.fill_path(end.qp, end.path, max_rd_atomic = 0)

            end.path.sqpsn = 1
            end.path.dqpsn = 1
            end.path.SGID_index = 0
            peerinfo = (dgid, dqpn, addr, rkey)

            end.path.MTU = 3
            end.path.rate = 0
            end.path.has_grh = True

            sgid = end.get_gid()

            end.peerinfo = peerinfo

            sqpn = end.qp.qp_num

            s.send(pickle.dumps((sgid, sqpn, end.mr.addr, end.mr.rkey)))

            print 'SGID=%s SQPN=%s DGID=%s DQPN=%s' % (sgid, sqpn, dgid, dqpn)

            end.connect(peerinfo)
            print s.recv(1024)
            
            s.shutdown(socket.SHUT_WR);
            s.recv(1024);

    return end

def cmd_rdma_bw(argv,o):
    """Perform a RDMA bandwidth test over a RC QP.
       Usage: %prog [SERVER]

       If SERVER is not specified then a server instance is started. A
       connection is made using TCP/IP sockets between the client and server
       process. This connection is used to exchange the connection
       information."""

    o.add_option("-C","--Ca",dest="CA",
                 help="RDMA device to use. Specify a device name or node GUID");
    o.add_option("-P","--Port",dest="port",
                 help="RDMA end port to use. Specify a GID, port GUID, DEVICE/PORT or port number.");
    o.add_option('-p', '--port', default=4444, type="int", dest="ip_port",
                 help="listen on/connect to port PORT")
    o.add_option('-6', '--ipv6', action="store_const",
                 const=socket.AF_INET6, dest="af", default=0,
                 help="use IPv6")
    o.add_option('-b', '--bidirectional', default=False, action="store_true",
                 help="measure bidirectional bandwidth")
    o.add_option('-d', '--ib-dev', metavar="DEV", dest="CA",
                 help="use IB device DEV")
    o.add_option('-i', '--ib-port', type="int", metavar="PORT", dest="port",
                 help="use port PORT of IB device")
    o.add_option('-s', '--size', default=1024*1024, type="int", metavar="BYTES",
                 help="exchange messages of size BYTES,(client only)")
    o.add_option('-e', '--num-sge', default=1, type="int", metavar="NUM",
                 help="Number of sges to use.")
    o.add_option('-t', '--tx-depth', default=100, type="int", help="number of exchanges")
    o.add_option('-n', '--iters', default=1000, type="int",
                 help="number of exchanges (client only)")
    o.add_option("--debug",dest="debug",action="count",default=0,
                 help="Increase the debug level, each -d increases by 1.")

    (args,values) = o.parse_args(argv);
    lib = LibIBOpts(o,args,1,(str,));

    if len(values) == 1:
        client_mode(values[0],args,lib.get_end_port())
    else:
        setup_server_endpoint(args.ip_port, args)
    return True;
