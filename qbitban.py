import time
import json
import asyncio
import aiohttp
import argparse
from collections import defaultdict, deque

class BanMonitor:
	def __init__(self, configuration):
		self.config = configuration
		self.session = None
		self.paused = False
		self.tracked_peers = set()

	async def connect(self):
		auth_data = {"username": self.config["username"], "password": self.config["password"]}
		for attempt in range(self.config["max_retries"]):
			try:
				async with self.session.post(f"{self.config['url']}/api/v2/auth/login", data=auth_data) as response:
					if response.status == 200:
						self.log("Connected to qBittorrent successfully.")
						return True
					self.log(f"Connection attempt {attempt + 1} failed.", level="ERROR")
			except aiohttp.ClientError as e:
				self.log(f"Connection error: {e}", level="ERROR")
			if attempt < self.config["max_retries"] - 1:
				await asyncio.sleep(self.config["retry_delay"])
		raise Exception("Failed to connect to qBittorrent after multiple attempts")
	
	async def fetch(self, endpoint, params=None):
		url = f"{self.config['url']}/{endpoint}"
		async with self.session.get(url, params=params) as response:
			response.raise_for_status()
			content_type = response.headers.get('Content-Type')
			
			if 'application/json' in content_type:
				return await response.json()
			elif 'text/plain' in content_type:
				return await response.text()
			else:
				raise ValueError(f"Unexpected content type: {content_type}")

	async def uploading_torrents(self):
		torrents = await self.fetch("api/v2/torrents/info", {"filter": "active"})
		return [
			torrent for torrent in torrents if torrent["state"] in {"uploading"} and torrent["upspeed"] > 0
		]

	async def speed_limit(self):
		try:
			speed_limit_enabled = int(await self.fetch("api/v2/transfer/speedLimitsMode")) ==1
			
			if speed_limit_enabled:
				upspeed_limit = int(await self.fetch("api/v2/transfer/uploadLimit"))
				
				if upspeed_limit < self.config["upspeed_threshold"]:
					self.log(f"Alternative speeds enabled and set to {upspeed_limit / 1024:.2f} KiB/s. Pausing monitoring...", level="WARN")
					self.paused = True
					
					while True:
						speed_limit_enabled = int(await self.fetch("api/v2/transfer/speedLimitsMode")) ==1
						upspeed_limit = int(await self.fetch("api/v2/transfer/uploadLimit"))
						
						if not speed_limit_enabled or upspeed_limit >= self.config["upspeed_threshold"]:
							self.log("Alternative speeds disabled or raised. Resuming monitoring...", level="WARN")
							self.paused = False
						break
						
						await asyncio.sleep(self.config["check_interval"])
			else:
				self.paused = False
		
		except Exception as e:
			self.log(f"Error checking alternative speeds: {e}", level="ERROR")
			self.paused = False

	async def track_speeds(self, torrent_hash):
		peer_speeds = defaultdict(lambda: deque(maxlen=self.config["upspeed_samples"]))
		
		try:
			initial_peers_data = await self.fetch("api/v2/sync/torrentPeers", {"hash": torrent_hash})
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
				
				for i in range(self.config["upspeed_samples"]):
					try:
						peers_data = await self.fetch("api/v2/sync/torrentPeers", {"hash": torrent_hash})
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
					
					await asyncio.sleep(self.config["upspeed_interval"])
			
		except Exception as e:
			self.log(f"Error fetching initial peer list: {e}", level="ERROR")
			return {}
		
		peer_averages = {}
		for (ip, port), speeds in peer_speeds.items():
			if speeds:
				avg_speed = sum(speeds) / len(speeds)
				peer_averages[(ip, port)] = avg_speed
				
				if avg_speed > 0 and avg_speed < self.config["upspeed_threshold"]:
					self.log(f"Peer {ip}:{port} with average speed: {avg_speed / 1024:.2f} KB/s. Banning...")
				elif avg_speed > self.config["upspeed_threshold"]:
					self.log(f"Adding exception for {ip}:{port} with average speed {avg_speed / 1024:.2f} KB/s.")
					self.tracked_peers.add((ip, port))
		
		return peer_averages

	async def ban_peers(self, torrent_hash):
		peer_averages = await self.track_speeds(torrent_hash)
		
		for (ip, port), avg_speed in peer_averages.items():
			if avg_speed > 0 and avg_speed < self.config["upspeed_threshold"]:
				try:
					async with self.session.post(f"{self.config['url']}/api/v2/transfer/banPeers", data={"peers": f"{ip}:{port}"}) as response:
						if response.status == 200:
							self.log(f"Banned peer {ip}:{port} with average upload speed {avg_speed / 1024:.2f} KB/s.")
						else:
							self.log(f"Failed to ban peer {ip}:{port}: {response.status}", level="ERROR")
				except Exception as e:
					self.log(f"Failed to ban peer {ip}:{port}: {e}", level="ERROR")

	async def monitor_peers(self):
		while True:
			await self.speed_limit()
			
			if not self.paused:
				active_torrents = await self.uploading_torrents()
				if active_torrents:
					await asyncio.gather(*(self.ban_peers(torrent["hash"]) for torrent in active_torrents))
				
			await asyncio.sleep(self.config["check_interval"])

	async def reset_tracked_peers(self):
		while True:
			await asyncio.sleep(self.config["reset_interval"])
			self.log("Resetting exceptions for tracked peers.", level="WARN")
			self.tracked_peers.clear()

	async def main(self):
		async with aiohttp.ClientSession() as session:
			self.session = session
			await self.connect()
			await asyncio.gather(
				self.monitor_peers(),
				self.reset_tracked_peers(),
			)
	
	def log(self, message, level="INFO"):
		with open(self.config["log_file_path"], "a") as log_file:
			log_file.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {level}: {message}\n")

if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="qbitban configuration")
	parser.add_argument("--config", type=str, required=True)
	args = parser.parse_args()
	
	with open(args.config, 'r') as config_file:
		configuration = json.load(config_file)
	
	qbitban = BanMonitor(configuration)
	asyncio.run(qbitban.main())