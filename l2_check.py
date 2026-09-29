#!/usr/bin/env python3
"""L2 深度检测：libtorrent 短会话（由 server.py 作为子进程调用，每任务一次）
- 75s 硬超时 + 10KB/s 上/下行 + 连接≤40
- 临时目录 /tmp/tt-<pid>，结束时自动删除
- 输出单行 JSON 后退出，绝不驻留"""
import libtorrent as lt, time, sys, os, shutil, json

target = sys.argv[1]
dl_dir = "/tmp/tt-" + str(os.getpid())
os.makedirs(dl_dir, exist_ok=True)

s = lt.session({
    "listen_interfaces": "0.0.0.0:%d" % (12040 + os.getpid() % 1000),
    "enable_dht": True,
    "download_rate_limit": 10 * 1024,
    "upload_rate_limit": 10 * 1024,
    "active_downloads": 1, "active_seeds": 0, "active_limit": 1,
    "connections_limit": 40,
})
s.add_dht_node(("router.bittorrent.com", 6881))
s.add_dht_node(("dht.transmissionbt.com", 6881))
s.add_dht_node(("router.utorrent.com", 6881))

atp = lt.parse_magnet_uri(target)
atp.save_path = dl_dir
atp.download_limit = 10 * 1024
atp.upload_limit = 10 * 1024
t = s.add_torrent(atp)

start = time.time()
tried_stop = False
while time.time() - start < 75:
    st = t.status()
    if st.has_metadata:
        time.sleep(12)
        break
    if time.time() - start > 50 and not tried_stop:
        tried_stop = True
        time.sleep(10)  # extra grace window for peers to arrive
        break
    time.sleep(2)

st = t.status()
pi = t.get_peer_info()
out = {
    "elapsed_sec": round(time.time() - start, 1),
    "dht_nodes": s.status().dht_nodes,
    "peers_seen": len(pi),
    "num_peers": st.num_peers,
    "num_seeds": st.num_seeds,
    "connect_candidates": getattr(st, "connect_candidates", 0),
    "metadata": bool(st.has_metadata),
    "metadata_files": len(t.get_torrent_info().files()) if st.has_metadata else None,
    "trackers_announced": bool(any(x.get("num_peers", 0) > 0 for x in t.trackers())),
    "downloaded_bytes": st.total_wanted_done,
}
print(json.dumps(out))

if hasattr(lt, "torrent_flags"):
    t.set_flags(lt.torrent_flags.paused)
else:
    t.pause()
s.pause()
shutil.rmtree(dl_dir, ignore_errors=True)
