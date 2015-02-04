#!/usr/bin/python

import errno
import random
from socket import *  # pylint: disable=wildcard-import
import time
import unittest

from scapy import all as scapy

import iproute
import mark_test
import net_test
import sendmsg

# Setsockopt values.
IPV6_ADDR_PREFERENCES = 72
IPV6_PREFER_SRC_PUBLIC = 0x0002


class IPv6SourceAddressSelectionTest(mark_test.MultiNetworkTest):

  def SetDAD(self, ifname, value):
    self.SetSysctl("/proc/sys/net/ipv6/conf/%s/accept_dad" % ifname, value)
    self.SetSysctl("/proc/sys/net/ipv6/conf/%s/dad_transmits" % ifname, value)

  def SetOptimisticDAD(self, ifname, value):
    self.SetSysctl("/proc/sys/net/ipv6/conf/%s/optimistic_dad" % ifname, value)

  def SetUseTempaddrs(self, ifname, value):
    self.SetSysctl("/proc/sys/net/ipv6/conf/%s/use_tempaddr" % ifname, value)

  def SetUseOptimistic(self, ifname, value):
    self.SetSysctl("/proc/sys/net/ipv6/conf/%s/use_optimistic" % ifname, value)

  def GetSourceIP(self, netid, mode="mark"):
    s = self.BuildSocket(6, net_test.UDPSocket, netid, mode)
    # Because why not...testing for temporary addresses is a separate thing.
    s.setsockopt(IPPROTO_IPV6, IPV6_ADDR_PREFERENCES, IPV6_PREFER_SRC_PUBLIC)

    s.connect((net_test.IPV6_ADDR, 123))
    src_addr = s.getsockname()[0]
    self.assertTrue(src_addr)
    return src_addr

  def assertAddressNotPresent(self, address):
    self.assertRaises(IOError, self.iproute.GetAddress, address)

  def assertAddressHasExpectedAttributes(
      self, address, expected_ifindex, expected_flags):
    ifa_msg = self.iproute.GetAddress(address)[0]
    self.assertEquals(AF_INET6 if ":" in address else AF_INET, ifa_msg.family)
    self.assertEquals(64, ifa_msg.prefixlen)
    self.assertEquals(iproute.RT_SCOPE_UNIVERSE, ifa_msg.scope)
    self.assertEquals(expected_ifindex, ifa_msg.index)
    self.assertEquals(expected_flags, ifa_msg.flags & expected_flags)

  def AddressIsTentative(self, address):
    ifa_msg = self.iproute.GetAddress(address)[0]
    return ifa_msg.flags & iproute.IFA_F_TENTATIVE

  def BindToAddress(self, address):
    s = net_test.UDPSocket(AF_INET6)
    s.bind((address, 0, 0, 0))

  def SendWithSourceAddress(self, address, netid):
    pktinfo = mark_test.MakePktInfo(6, address, 0)
    cmsgs = [(net_test.SOL_IPV6, IPV6_PKTINFO, pktinfo)]
    s = self.BuildSocket(6, net_test.UDPSocket, netid, "mark")
    return sendmsg.Sendmsg(s, (net_test.IPV6_ADDR, 53), "Hello", cmsgs, 0)

  def assertAddressUsable(self, address, netid):
    self.BindToAddress(address)
    self.SendWithSourceAddress(address, netid)
    # No exceptions? Good.

  def assertAddressNotUsable(self, address, netid):
    self.assertRaisesErrno(errno.EADDRNOTAVAIL, self.BindToAddress, address)
    self.assertRaisesErrno(errno.EINVAL,
                           self.SendWithSourceAddress, address, netid)

  def assertAddressSelected(self, address, netid):
    self.assertEquals(address, self.GetSourceIP(netid))

  def assertAddressNotSelected(self, address, netid):
    self.assertNotEquals(address, self.GetSourceIP(netid))

  def WaitForDad(self, address):
    for _ in xrange(20):
      if not self.AddressIsTentative(address):
        return
      time.sleep(0.1)
    raise AssertionError("%s did not complete DAD after 2 seconds")


class MultiInterfaceSourceAddressSelectionTest(IPv6SourceAddressSelectionTest):

  def setUp(self):
    # [0]  Make sure DAD, optimistic DAD, and the use_optimistic option
    # are all consistently disabled at the outset.
    for netid in self.tuns:
      self.SetDAD(self.GetInterfaceName(netid), 0)
      self.SetOptimisticDAD(self.GetInterfaceName(netid), 0)
      self.SetUseTempaddrs(self.GetInterfaceName(netid), 0)
      self.SetUseOptimistic(self.GetInterfaceName(netid), 0)

    # [1]  Pick an interface on which to test.
    self.test_netid = random.choice(self.tuns.keys())
    self.test_ip = self.MyAddress(6, self.test_netid)
    self.test_ifindex = self.ifindices[self.test_netid]
    self.test_ifname = self.GetInterfaceName(self.test_netid)

    # [2]  Delete the test interface's IPv6 address.
    self.iproute.DelAddress(self.test_ip, 64, self.test_ifindex)
    self.assertAddressNotPresent(self.test_ip)

    self.assertAddressNotUsable(self.test_ip, self.test_netid)


class TentativeAddressTest(MultiInterfaceSourceAddressSelectionTest):

  def testRfc6724Behaviour(self):
    # [3]  Get an IPv6 address back, in DAD start-up.
    self.SetDAD(self.test_ifname, 1)  # Enable DAD
    # Send a RA to start SLAAC and subsequent DAD.
    self.SendRA(self.test_netid, 0)
    # Get flags and prove tentative-ness.
    self.assertAddressHasExpectedAttributes(
        self.test_ip, self.test_ifindex, iproute.IFA_F_TENTATIVE)

    # Even though the interface has an IPv6 address, its tentative nature
    # prevents it from being selected.
    self.assertAddressNotUsable(self.test_ip, self.test_netid)
    self.assertAddressNotSelected(self.test_ip, self.test_netid)

    # Busy wait for DAD to complete (should be less than 1 second).
    self.WaitForDad(self.test_ip)

    # The test_ip should have completed DAD by now, and should be the
    # chosen source address, eligible to bind to, etc.
    self.assertAddressUsable(self.test_ip, self.test_netid)
    self.assertAddressSelected(self.test_ip, self.test_netid)


class OptimisticAddressTest(MultiInterfaceSourceAddressSelectionTest):

  def testRfc6724Behaviour(self):
    # [3]  Get an IPv6 address back, in optimistic DAD start-up.
    self.SetDAD(self.test_ifname, 1)  # Enable DAD
    self.SetOptimisticDAD(self.test_ifname, 1)
    # Send a RA to start SLAAC and subsequent DAD.
    self.SendRA(self.test_netid, 0)
    # Get flags and prove optimism.
    self.assertAddressHasExpectedAttributes(
        self.test_ip, self.test_ifindex, iproute.IFA_F_OPTIMISTIC)

    # Optimistic addresses are usable but are not selected.
    if mark_test.LinuxVersion() >= (3, 19, 0):
      # The version checked in to android kernels <= 3.10 requires the
      # use_optimistic sysctl to be turned on.
      self.assertAddressUsable(self.test_ip, self.test_netid)
    self.assertAddressNotSelected(self.test_ip, self.test_netid)

    # Busy wait for DAD to complete (should be less than 1 second).
    self.WaitForDad(self.test_ip)

    # The test_ip should have completed DAD by now, and should be the
    # chosen source address.
    self.assertAddressUsable(self.test_ip, self.test_netid)
    self.assertAddressSelected(self.test_ip, self.test_netid)


class OptimisticAddressOkayTest(MultiInterfaceSourceAddressSelectionTest):

  def testModifiedRfc6724Behaviour(self):
    # [3]  Get an IPv6 address back, in optimistic DAD start-up.
    self.SetDAD(self.test_ifname, 1)  # Enable DAD
    self.SetOptimisticDAD(self.test_ifname, 1)
    self.SetUseOptimistic(self.test_ifname, 1)
    # Send a RA to start SLAAC and subsequent DAD.
    self.SendRA(self.test_netid, 0)
    # Get flags and prove optimistism.
    self.assertAddressHasExpectedAttributes(
        self.test_ip, self.test_ifindex, iproute.IFA_F_OPTIMISTIC)

    # The interface has an IPv6 address and, despite its optimistic nature,
    # the use_optimistic option allows it to be selected.
    self.assertAddressUsable(self.test_ip, self.test_netid)
    self.assertAddressSelected(self.test_ip, self.test_netid)


class ValidBeforeOptimisticTest(MultiInterfaceSourceAddressSelectionTest):

  def testModifiedRfc6724Behaviour(self):
    # [3]  Add a valid IPv6 address to this interface and verify it is
    # selected as the source address.
    preferred_ip = self.IPv6Prefix(self.test_netid) + "cafe"
    self.iproute.AddAddress(preferred_ip, 64, self.test_ifindex)
    self.assertAddressHasExpectedAttributes(
        preferred_ip, self.test_ifindex, iproute.IFA_F_PERMANENT)
    self.assertEquals(preferred_ip, self.GetSourceIP(self.test_netid))

    # [4]  Get another IPv6 address, in optimistic DAD start-up.
    self.SetDAD(self.test_ifname, 1)  # Enable DAD
    self.SetOptimisticDAD(self.test_ifname, 1)
    self.SetUseOptimistic(self.test_ifname, 1)
    # Send a RA to start SLAAC and subsequent DAD.
    self.SendRA(self.test_netid, 0)
    # Get flags and prove optimism.
    self.assertAddressHasExpectedAttributes(
        self.test_ip, self.test_ifindex, iproute.IFA_F_OPTIMISTIC)

    # Since the interface has another IPv6 address, the optimistic address
    # is not selected--the other, valid address is chosen.
    self.assertAddressUsable(self.test_ip, self.test_netid)
    self.assertAddressNotSelected(self.test_ip, self.test_netid)
    self.assertAddressSelected(preferred_ip, self.test_netid)


class DadFailureTest(MultiInterfaceSourceAddressSelectionTest):

  def testDadFailure(self):
    # [3]  Get an IPv6 address back, in optimistic DAD start-up.
    self.SetDAD(self.test_ifname, 1)  # Enable DAD
    self.SetOptimisticDAD(self.test_ifname, 1)
    self.SetUseOptimistic(self.test_ifname, 1)
    # Send a RA to start SLAAC and subsequent DAD.
    self.SendRA(self.test_netid, 0)
    # Prove optimism and usability.
    self.assertAddressHasExpectedAttributes(
        self.test_ip, self.test_ifindex, iproute.IFA_F_OPTIMISTIC)
    self.assertAddressUsable(self.test_ip, self.test_netid)
    self.assertAddressSelected(self.test_ip, self.test_netid)

    # Send a NA for the optimistic address, indicating address conflict
    # ("DAD defense").
    conflict_macaddr = "02:00:0b:ad:d0:0d"
    dad_defense = (scapy.Ether(src=conflict_macaddr, dst="33:33:33:00:00:01") /
                   scapy.IPv6(src=self.test_ip, dst="ff02::1") /
                   scapy.ICMPv6ND_NA(tgt=self.test_ip, R=0, S=0, O=1) /
                   scapy.ICMPv6NDOptDstLLAddr(lladdr=conflict_macaddr))
    self.ReceiveEtherPacketOn(self.test_netid, dad_defense)

    # The address should have failed DAD, and therefore no longer be usable.
    self.assertAddressNotUsable(self.test_ip, self.test_netid)
    self.assertAddressNotSelected(self.test_ip, self.test_netid)

    # TODO(ek): verify that an RTM_DELADDR issued for the DAD-failed address.


# TODO(ek): add tests listening for netlink events.


if __name__ == "__main__":
  unittest.main()