/home/<your_user>/kizumi/venv/bin/pip install --upgrade nodriver

iconv -f latin-1 -t utf-8 \
  /home/<your_user>/kizumi/venv/lib/python3.14/site-packages/nodriver/cdp/network.py \
  -o /tmp/network_fixed.py \
  && mv /tmp/network_fixed.py \
  /home/<your_user>/kizumi/venv/lib/python3.14/site-packages/nodriver/cdp/network.py

  sed -i '1s/^/# -*- coding: latin-1 -*-\n/' \
  /home/<your_user>/kizumi/venv/lib/python3.14/site-packages/nodriver/cdp/network.py
