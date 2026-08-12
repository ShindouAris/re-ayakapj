$HOME/re-ayakapj/venv/bin/pip install --upgrade nodriver

iconv -f latin-1 -t utf-8 \
  $HOME/re-ayakapj/venv/lib/python3.14/site-packages/nodriver/cdp/network.py \
  -o /tmp/network_fixed.py \
  && mv /tmp/network_fixed.py \
  $HOME/re-ayakapj/venv/lib/python3.14/site-packages/nodriver/cdp/network.py

sed -i '1s/^/# -*- coding: latin-1 -*-\n/' \
  $HOME/re-ayakapj/venv/lib/python3.14/site-packages/nodriver/cdp/network.py
