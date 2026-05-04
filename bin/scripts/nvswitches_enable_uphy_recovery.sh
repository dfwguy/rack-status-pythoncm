#!/bin/bash
#

export SSHPASS='WelcomeDell1!'

sshpass -e pdsh -w $1 -R ssh "nv set fae int acp1-144 link phy-recovery serdes-eq-mode enabled;
                              nv config apply -y;
			      nv config save;"

#check
#
# before ct reset/reboot
#   p3-r15-nvsw-08: serdes-eq-mode     disabled     enabled
#   p3-r15-nvsw-08: serdes-eq-timeout  0            0
#
# after ct reset/reboot
#   p3-r15-nvsw-08: serdes-eq-mode     enabled     enabled
#   p3-r15-nvsw-08: serdes-eq-timeout  100          0
#sshpass -e pdsh -w $1 -R ssh "seq 1 144 | xargs -i -t nv show fae int acp{} link phy-recovery;"
