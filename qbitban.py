import time
import json
import asyncio
import aiohttp
import argparse
from collections import defaultdict, deque

class QBitClient:
	def __init__(self, url, username, password, max_retries, retry_delay):
		self.url = url
		self.auth = {"username": username, "password": password}
		self.max_retries = max_retries
		self.retry_delay = retry_delay
		self.session = None

	async def __aenter__(self):
		self.session = aiohttp.ClientSession()
		return self

	async def __aexit__(self, exc_type, exc_val, exc_tb):
		if self.session:
			await self.session.close()

	async def connect(self):
		for attempt in range(self.max_retries):
			try:
				async with self.session.post(f"{self.url}/api/v2/auth/login", data=self.auth) as response:
					if response.status == 200:
						return True
			except aiohttp.ClientError:
				if attempt < self.max_retries - 1:
					await asyncio.sleep(self.retry_delay)
		raise Exception("Failed to connect to qBittorrent after multiple attempts")

	async def fetch(self, endpoint, params=None):
		url = f"{self.url}/{endpoint}"
		async with self.session.get(url, params=params) as response:
			response.raise_for_status()
			content_type = response.headers.get('Content-Type')
			
			if 'application/json' in content_type:
				return await response.json()
			elif 'text/plain' in content_type:
				return await response.text()
			else:
				raise ValueError(f"Unexpected content type: {content_type}")

class PeerTracker:
	def __init__(self, client, logger, reset_interval, upspeed_samples, upspeed_interval):
		self.client = client
		self.log = logger
		self.reset_interval = reset_interval
		self.upspeed_samples = upspeed_samples
		self.upspeed_interval = upspeed_interval
		self.tracked_peers = set()

	async def track_speeds(self, torrent_hash):
		peer_speeds = defaultdict(lambda: deque(maxlen=self.upspeed_samples))
		try:
			initial_peers_data = await self.client.fetch("api/v2/sync/torrentPeers", {"hash": torrent_hash})
			initial_peers = initial_peers_data.get("peers", {})
			if not initial_peers:
				return {}
			
			for peer_id, peer_info in initial_peers.items():
				ip = peer_info["ip"]
				port = peer_info.get("port", 6881)
				if (ip, port) in self.tracked_peers:
					continue
				
				initial_speed = peer_info.get("up_speed", 0)
				if initial_speed == 0:
					self.log(f"Dropping peer {ip}:{port} due to invalid speed.", level="WARN")
					continue
				
				for i in range(self.upspeed_samples):
					try:
						peers_data = await self.client.fetch("api/v2/sync/torrentPeers", {"hash": torrent_hash})
						peers = peers_data.get("peers", {})
						if peer_id not in peers:
							self.log(f"Peer {ip}:{port} is no longer present at iteration {i}. Discarding...", level="WARN")
							if (ip, port) in peer_speeds:
								del peer_speeds[(ip, port)]
							break
						
						if i == 0:
							self.log(f"Tracking {ip}:{port} for torrent: {torrent_hash}")
						
						current_speed = peers[peer_id]["up_speed"]
						peer_speeds[(ip, port)].append(current_speed)
					
					except Exception as e:
						self.log(f"Error fetching/updating peer data: {e}", level="ERROR")
						return {}
					
					await asyncio.sleep(self.upspeed_interval)
			
			return {
				peer: sum(speeds) / len(speeds)
				for peer, speeds in peer_speeds.items()
				if speeds
			}
		
		except Exception as e:
			self.log(f"Error fetching initial peer list: {e}", level="ERROR")
			return {}

	async def reset_tracked_peers(self):
		while True:
			await asyncio.sleep(self.reset_interval)
			self.log("Resetting exceptions for tracked peers.", level="WARN")
			self.tracked_peers.clear()

class BanMonitor:
	def __init__(self, client, logger, peer_tracker, check_interval, upspeed_threshold):
		self.client = client
		self.log = logger
		self.peer_tracker = peer_tracker
		self.check_interval = check_interval
		self.upspeed_threshold = upspeed_threshold

	async def speed_limit(self):
		try:
			speed_limit_enabled = int(await self.client.fetch("api/v2/transfer/speedLimitsMode")) == 1
			if speed_limit_enabled:
				upspeed_limit = int(await self.client.fetch("api/v2/transfer/uploadLimit"))
				if upspeed_limit < self.upspeed_threshold:
					self.log(f"Speed limit enabled and set to {upspeed_limit / 1024:.2f} KiB/s. Pausing monitoring...", level="WARN")
					while True:
						speed_limit_enabled = int(await self.client.fetch("api/v2/transfer/speedLimitsMode")) == 1
						if not speed_limit_enabled:
							self.log("Speed limit disabled. Resuming monitoring...", level="WARN")
							return False
						upspeed_limit = int(await self.client.fetch("api/v2/transfer/uploadLimit"))
						if upspeed_limit >= self.upspeed_threshold:
							self.log("Speed limit raised. Resuming monitoring...", level="WARN")
							return False
						await asyncio.sleep(self.check_interval)
			return False
		
		except Exception as e:
			self.log(f"Error checking alternative speeds: {e}", level="ERROR")
			return False

	async def uploading_torrents(self):
		torrents = await self.client.fetch("api/v2/torrents/info", {"filter": "active"})
		return [
			torrent for torrent in torrents
			if torrent["state"] in {"uploading"} and torrent["upspeed"] > 0
		]

	async def peer_monitor(self, torrent_hash):
		peer_averages = await self.peer_tracker.track_speeds(torrent_hash)
		
		for (ip, port), avg_speed in peer_averages.items():
			if 0 < avg_speed < self.upspeed_threshold:
				if await self.ban_peer(ip, port):
					self.log(f"Banned peer {ip}:{port} with average upload speed {avg_speed / 1024:.2f} KB/s")
				else:
					self.log(f"Failed to ban peer {ip}:{port}", level="ERROR")
			elif avg_speed >= self.upspeed_threshold:
				self.log(f"Adding exception for {ip}:{port} with average speed {avg_speed / 1024:.2f} KB/s")
				self.peer_tracker.tracked_peers.add((ip, port))

	async def ban_peer(self, ip, port):
		try:
			async with self.client.session.post(f"{self.client.url}/api/v2/transfer/banPeers", data={"peers": f"{ip}:{port}"}) as response:
				return response.status == 200
		except Exception:
			return False

	async def torrent_monitor(self):
		while True:
			try:
				if not await self.speed_limit():
					active_torrents = await self.uploading_torrents()
					if active_torrents:
						await asyncio.gather(*(self.peer_monitor(torrent["hash"]) for torrent in active_torrents))
				await asyncio.sleep(self.check_interval)
			
			except Exception as e:
				self.log(f"Error in ban monitor: {str(e)}", level="ERROR")

class Qbitban:
	def __init__(self, config_path):
		with open(config_path, 'r') as config_file:
			self.config = json.load(config_file)
		
		self.client = QBitClient(
			url = self.config["url"],
			username = self.config["username"],
			password = self.config["password"],
			max_retries = self.config["max_retries"],
			retry_delay = self.config["retry_delay"]
		)
		
		self.peer_tracker = PeerTracker(
			client = self.client,
			logger = self.log,
			reset_interval = self.config["reset_interval"],
			upspeed_samples = self.config["upspeed_samples"],
			upspeed_interval = self.config["upspeed_interval"]
		)
		
		self.ban_monitor = BanMonitor(
			client = self.client,
			logger = self.log,
			peer_tracker = self.peer_tracker,
			check_interval = self.config["check_interval"],
			upspeed_threshold = self.config["upspeed_threshold"]
		)
		
		self.log_file_path = self.config["log_file_path"]

	def log(self, message, level="INFO"):
		with open(self.log_file_path, "a") as log_file:
			log_file.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {level}: {message}\n")

	async def main(self):
		async with aiohttp.ClientSession() as session:
			try:
				self.client.session = session
				await self.client.connect()
				self.log("Connected to qBittorrent successfully.")
				
				await asyncio.gather(
					self.ban_monitor.torrent_monitor(),
					self.peer_tracker.reset_tracked_peers()
				)
			
			except Exception as e:
				self.log(f"Critical error: {str(e)}", level="ERROR")
				raise

if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="qbitban configuration")
	parser.add_argument("--config", type=str, required=True)
	args = parser.parse_args()
	
	qbitban = Qbitban(args.config)
	asyncio.run(qbitban.main())