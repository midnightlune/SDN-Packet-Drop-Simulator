from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, udp


class SimpleDropController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # Priorities
    PRIORITY_TABLE_MISS = 0
    PRIORITY_FORWARD    = 100
    PRIORITY_DROP       = 300   # Must be > FORWARD so drop rules always win

    def __init__(self, *args, **kwargs):
        super(SimpleDropController, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        # Registry of installed drop rules for regression validation
        # { dpid: [ (description, match_dict), ... ] }
        self.drop_rules_registry = {}

    # Switch connects 
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self.mac_to_port[dp.id] = {}
        self.drop_rules_registry[dp.id] = []

        # Order matters- drop rules first, then table-miss last
        self.install_drop_rules(dp)
        self.install_table_miss(dp)

        self.logger.info("=== Switch %s connected: drop rules installed ===", dp.id)

    # Table-miss: send unknown packets to controller
    def install_table_miss(self, dp):
        ofp    = dp.ofproto
        parser = dp.ofproto_parser
        match  = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                          ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, self.PRIORITY_TABLE_MISS, match, actions)

    # Drop rules 
    def install_drop_rules(self, dp):
        parser = dp.ofproto_parser
        dpid   = dp.id

        # RULE 1: h1 (10.0.0.1) → h3 (10.0.0.3) — drop all IP traffic
        match1 = parser.OFPMatch(
            eth_type=0x0800,
            ipv4_src="10.0.0.1",
            ipv4_dst="10.0.0.3"
        )
        self.add_flow(dp, self.PRIORITY_DROP, match1, [])
        self.drop_rules_registry[dpid].append(
            ("DROP h1→h3 all IP", {"ipv4_src": "10.0.0.1", "ipv4_dst": "10.0.0.3"})
        )
        self.logger.info("Installing DROP: h1 → h3 (all IP traffic)")

        # RULE 2: h2 (10.0.0.2) → h3 (10.0.0.3) UDP dst-port 5001
        match2 = parser.OFPMatch(
            eth_type=0x0800,
            ip_proto=17,            # UDP  (prerequisite for udp_dst)
            ipv4_src="10.0.0.2",
            ipv4_dst="10.0.0.3",
            udp_dst=5001
        )
        self.add_flow(dp, self.PRIORITY_DROP, match2, [])
        self.drop_rules_registry[dpid].append(
            ("DROP h2→h3 UDP 5001",
             {"ipv4_src": "10.0.0.2", "ipv4_dst": "10.0.0.3",
              "ip_proto": 17, "udp_dst": 5001})
        )
        self.logger.info("Installing DROP: h2 → h3 UDP dst-port 5001")

    # Packet-in: learning switch
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg    = ev.msg
        dp     = msg.datapath
        ofp    = dp.ofproto
        parser = dp.ofproto_parser

        dpid     = dp.id
        in_port  = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        dst = eth.dst
        src = eth.src

        # Learn MAC -> port
        self.mac_to_port[dpid][src] = in_port

        # Decide output port
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofp.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        # Install forwarding rule at PRIORITY_FORWARD (100).
        # Drop rules sit at PRIORITY_DROP (300) and always win.
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_src=src, eth_dst=dst)
            self.add_flow(dp, self.PRIORITY_FORWARD, match, actions)

        # Send this packet out
        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=msg.data
        )
        dp.send_msg(out)

    # Flow-stats reply: used by regression test
    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        dp   = ev.msg.datapath
        dpid = dp.id
        body = ev.msg.body

        self.logger.info("=== REGRESSION CHECK — Switch %s ===", dpid)

        installed_matches = []
        for stat in body:
            installed_matches.append(dict(stat.match))

        expected = self.drop_rules_registry.get(dpid, [])
        all_pass = True

        for desc, expected_fields in expected:
            found = any(
                all(installed.get(k) == v for k, v in expected_fields.items())
                for installed in installed_matches
            )
            status = "PASS" if found else "FAIL"
            if not found:
                all_pass = False
            self.logger.info("  [%s] %s", status, desc)

        if all_pass:
            self.logger.info("  All drop rules verified present.")
        else:
            self.logger.warning("  One or more drop rules MISSING — check switch!")

    # Regression trigger: request flow stats
    def run_regression(self, dp):
        """
         to verify drop rules are still installed on the switch.
        Result is logged via flow_stats_reply_handler.
        """
        parser  = dp.ofproto_parser
        ofp     = dp.ofproto
        req = parser.OFPFlowStatsRequest(dp, table_id=ofp.OFPTT_ALL)
        dp.send_msg(req)

    # Helper
    def add_flow(self, dp, priority, match, actions,
                 idle_timeout=0, hard_timeout=0):
        """
        idle_timeout=0, hard_timeout=0: rules never expire.
        This prevents drop rules disappearing silently after idle periods.
        """
        parser = dp.ofproto_parser
        ofp    = dp.ofproto

        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]

        mod = parser.OFPFlowMod(
            datapath=dp,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout
        )
        dp.send_msg(mod)