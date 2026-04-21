from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel
import time


def run():
    net = Mininet(controller=RemoteController, switch=OVSSwitch)

    net.addController("c0", ip="127.0.0.1", port=6633)

    s1 = net.addSwitch("s1", protocols="OpenFlow13")

    h1 = net.addHost("h1", ip="10.0.0.1/24")
    h2 = net.addHost("h2", ip="10.0.0.2/24")
    h3 = net.addHost("h3", ip="10.0.0.3/24")
    h4 = net.addHost("h4", ip="10.0.0.4/24")

    net.addLink(h1, s1)
    net.addLink(h2, s1)
    net.addLink(h3, s1)
    net.addLink(h4, s1)

    net.start()

    print("\nWaiting 3 seconds for controller to install rules...")
    time.sleep(3)

    print('''
    Hosts : h1=10.0.0.1  h2=10.0.0.2  h3=10.0.0.3  h4=10.0.0.4
    Switch: s1 (OpenFlow 1.3)  Controller: 127.0.0.1:6633

    DROP RULES (priority 300):
    h1 -> h3  all IP traffic
    h2 -> h3  UDP dst-port 5001
''')

    CLI(net)
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    run()