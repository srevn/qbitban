import sys
import time
import json
import logging
import asyncio
import aiohttp
import argparse
import signal
from collections import defaultdict, deque

class QBitClient:
	def __init__(self, url, username, password, max_retries, retry_delay):
		self.url = url
		self.auth = {"username": username, "password": password}
		self.max_retries = max_retries
		self.retry_delay = retry_delay
		self.session = None

	async def connect(self):
		for attempt in range(self.max_retries):
			try:
				async with self.session.post(f"{self.url}/api/v2/auth/login", data=self.auth) as response:
					if response.status == 200:
						log.info("Connected to qBittorrent successfully.")
						return True
			except aiohttp.ClientConnectionError as e:
				log.error(f"Connection error: {str(e)}")
				if attempt < self.max_retries - 1:
					await asyncio.sleep(self.retry_delay)
		
		raise ConnectionError("Failed to connect to qBittorrent after multiple attempts.")

	async def fetch(self, endpoint, params=None):
		async with self.session.get(f"{self.url}/{endpoint}", params=params) as response:
			response.raise_for_status()
			content_type = response.headers.get('Content-Type')
			
			if 'application/json' in content_type:
				return await response.json()
			elif 'text/plain' in content_type:
				return await response.text()
			else:
				raise ValueError(f"Unexpected content type: {content_type}")

	async def close(self):
		if self.session:
			await self.session.close()
			self.session = None

class PeerTracker:
	def __init__(self, client, reset_interval, upspeed_samples, upspeed_interval):
		self.client = client
		self.reset_interval = reset_interval
		self.upspeed_samples = upspeed_samples
		self.upspeed_interval = upspeed_interval
		self.tracked_peers = set()

	def speed_analyzer(self, speeds, ema_weight=0.8, alpha=0.3):
		if not speeds:
			return 0
		
		try:
			speeds = list(speeds)
			sorted_speeds = sorted(speeds)
			n = len(sorted_speeds)
			
			if n < 4:
				q1, q3 = sorted_speeds[0], sorted_speeds[-1]
			else:
				q1 = sorted_speeds[n // 4]
				q3 = sorted_speeds[(3 * n) // 4]
			
			iqr = q3 - q1
			lower_bound = q1 - 1.5 * iqr
			upper_bound = q3 + 1.5 * iqr
			filtered = [s for s in speeds if lower_bound <= s <= upper_bound]
			filtered_mean = sum(filtered) / len(filtered) if filtered else 0
			
			ema = speeds[0]
			for speed in speeds[1:]:
				ema = alpha * speed + (1 - alpha) * ema
			
			combined_speed = (ema * ema_weight) + (filtered_mean * (1 - ema_weight))
			
			return combined_speed
		
		except (TypeError, IndexError) as e:
			log.error(f"Error while analyzing speed: {str(e)}")
			return 0

	async def track_speed(self, torrent_hash):
		peer_speeds = defaultdict(lambda: deque(maxlen=self.upspeed_samples))
		try:
			initial_peers_data = await self.client.fetch("api/v2/sync/torrentPeers", {"hash": torrent_hash})
			initial_peers = initial_peers_data.get("peers", {})
			if not initial_peers:
				log.debug(f"No initial peers for torrent: {torrent_hash}")
				return {}
			
			for peer_id, peer_info in initial_peers.items():
				ip = peer_info["ip"]
				port = peer_info.get("port", 6881)
				if (ip, port) in self.tracked_peers:
					log.debug(f"Skipping measurments for {ip}:{port} as it's listed in the exceptions.")
					continue
				
				initial_speed = peer_info.get("up_speed", 0)
				if initial_speed == 0:
					log.debug(f"Dropping peer {ip}:{port} due to null initial speed.")
					continue
				
				for i in range(self.upspeed_samples):
					try:
						peers_data = await self.client.fetch("api/v2/sync/torrentPeers", {"hash": torrent_hash})
						peers = peers_data.get("peers", {})
						if peer_id not in peers:
							log.warning(f"Peer {ip}:{port} is no longer present at iteration {i}. Discarding...")
							if (ip, port) in peer_speeds:
								del peer_speeds[(ip, port)]
							break
						
						if i == 0:
							log.info(f"Tracking {ip}:{port} for torrent: {torrent_hash}")
						
						current_speed = peers[peer_id]["up_speed"]
						peer_speeds[(ip, port)].append(current_speed)
					
					except Exception as e:
						log.error(f"Error fetching/updating peer data: {str(e)}")
						return {}
					
					await asyncio.sleep(self.upspeed_interval)
			
			return {
				peer: self.speed_analyzer(list(speeds))
				for peer, speeds in peer_speeds.items()
				if speeds
			}
		
		except Exception as e:
			log.error(f"Error while tracking peer speeds: {str(e)}")
			return {}

	async def reset_tracked_peers(self):
		while True:
			try:
				await asyncio.sleep(self.reset_interval)
				if asyncio.current_task().cancelled():
					break
				log.debug("Resetting exceptions for tracked peers.")
				self.tracked_peers.clear()
			
			except asyncio.CancelledError:
				raise

class BanMonitor:
	def __init__(self, client, peer_tracker, min_seeders, reset_interval, check_interval, upspeed_threshold):
		self.client = client
		self.peer_tracker = peer_tracker
		self.min_seeders = min_seeders
		self.reset_interval = reset_interval
		self.check_interval = check_interval
		self.upspeed_threshold = upspeed_threshold
		self.tracked_torrents = set()
		self.active_monitors = {}

	async def speed_limit(self):
		try:
			speed_limit_enabled = int(await self.client.fetch("api/v2/transfer/speedLimitsMode")) == 1
			
			if speed_limit_enabled:
				upspeed_limit = int(await self.client.fetch("api/v2/transfer/uploadLimit"))
				if upspeed_limit < self.upspeed_threshold:
					log.info(f"Speed limit enabled and set to {upspeed_limit / 1024:.2f} KB/s. Pausing...")
					
					while True:
						speed_limit_enabled = int(await self.client.fetch("api/v2/transfer/speedLimitsMode")) == 1
						if not speed_limit_enabled:
							log.info("Speed limit disabled. Resuming...")
							return False
						
						upspeed_limit = int(await self.client.fetch("api/v2/transfer/uploadLimit"))
						if upspeed_limit >= self.upspeed_threshold:
							log.info(f"Speed limit raised to {upspeed_limit / 1024:.2f} KB/s. Resuming...")
							return False
						
						await asyncio.sleep(self.check_interval)
			
			return False
		
		except Exception as e:
			log.error(f"Error checking alternative speed: {str(e)}")
			return False

	async def uploading_torrents(self):
		torrents = await self.client.fetch("api/v2/torrents/info", {"filter": "active"})
		
		for torrent in torrents:
			if not (torrent["state"] in {"uploading"} and torrent["upspeed"] > 0):
				continue
			
			torrent_hash = torrent["hash"]
			if torrent["num_complete"] >= self.min_seeders:
				self.tracked_torrents.discard(torrent_hash)
				yield torrent_hash
			elif torrent_hash not in self.tracked_torrents:
				log.info(f"Adding exception for {torrent_hash} with {torrent['num_complete']} "
						f"{'seeder' if torrent['num_complete'] == 1 else 'seeders'}.")
				self.tracked_torrents.add(torrent_hash)

	async def peer_monitor(self, torrent_hash):
		try:
			peer_averages = await self.peer_tracker.track_speed(torrent_hash)
			
			for (ip, port), avg_speed in peer_averages.items():
				if 0 < avg_speed < self.upspeed_threshold:
					if await self.ban_peer(ip, port):
						log.info(f"Banned peer {ip}:{port} with average upload speed {avg_speed / 1024:.2f} KB/s.")
					else:
						log.error(f"Failed to ban peer {ip}:{port}")
				
				elif avg_speed >= self.upspeed_threshold:
					log.info(f"Adding exception for {ip}:{port} with average speed {avg_speed / 1024:.2f} KB/s.")
					self.peer_tracker.tracked_peers.add((ip, port))
		
		finally:
			if torrent_hash in self.active_monitors:
				del self.active_monitors[torrent_hash]

	async def ban_peer(self, ip, port):
		try:
			async with self.client.session.post(f"{self.client.url}/api/v2/transfer/banPeers", data={"peers": f"{ip}:{port}"}) as response:
				return response.status == 200
		
		except Exception as e:
			log.error(f"An error occurred while sending peer data: {str(e)}")
			return False

	async def torrent_monitor(self):
		while True:
			try:
				if asyncio.current_task().cancelled():
					break
				
				if not await self.speed_limit():
					async for torrent_hash in self.uploading_torrents():
						
						if torrent_hash in self.active_monitors:
							if not self.active_monitors[torrent_hash].done():
								log.debug(f"Monitoring {torrent_hash} in progress")
								continue
							
							log.debug(f"Monitoring completed for {torrent_hash}")
							del self.active_monitors[torrent_hash]
						
						log.debug(f"Starting new monitoring task for {torrent_hash}")
						task = asyncio.create_task(self.peer_monitor(torrent_hash))
						self.active_monitors[torrent_hash] = task
					
				await asyncio.sleep(self.check_interval)
				
			except asyncio.CancelledError:
				raise
			
			except Exception as e:
				log.error(f"An error occurred during torrent monitoring: {str(e)}")

	async def reset_tracked_torrents(self):
		while True:
			try:
				await asyncio.sleep(self.reset_interval)
				if asyncio.current_task().cancelled():
					break
				log.debug("Resetting exceptions for tracked torrents.")
				self.tracked_torrents.clear()
			
			except asyncio.CancelledError:
				raise

class Qbitban:
	def __init__(self, config_path):
		with open(config_path, 'r') as config_file:
			self.config = json.load(config_file)
		self.logger()
		
		self.client = QBitClient(
			url = self.config["url"],
			username = self.config["username"],
			password = self.config["password"],
			max_retries = self.config["max_retries"],
			retry_delay = self.config["retry_delay"]
		)
		
		self.peer_tracker = PeerTracker(
			client = self.client,
			reset_interval = self.config["reset_interval"],
			upspeed_samples = self.config["upspeed_samples"],
			upspeed_interval = self.config["upspeed_interval"]
		)
		
		self.ban_monitor = BanMonitor(
			client = self.client,
			peer_tracker = self.peer_tracker,
			min_seeders = self.config["min_seeders"],
			reset_interval = self.config["reset_interval"],
			check_interval = self.config["check_interval"],
			upspeed_threshold = self.config["upspeed_threshold"]
		)
	
	def logger(self):
		try:
			log.setLevel(logging.DEBUG)
			log_file = self.config["log_file"]
			
			format = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', 
			datefmt = '%Y-%m-%d %H:%M:%S')
			
			file_handler = logging.FileHandler(log_file)
			file_handler.setLevel(logging.INFO)
			file_handler.setFormatter(format)
			log.addHandler(file_handler)
			
			if not getattr(sys, 'frozen', False):
				console_handler = logging.StreamHandler()
				console_handler.setLevel(logging.DEBUG)
				console_handler.setFormatter(format)
				log.addHandler(console_handler)
		
		except Exception as e:
			print(f"Misconfiguration in logging setup: {str(e)}", file=sys.stderr)
			raise

	async def main(self):
		self.shutdown_event = asyncio.Event()
		loop = asyncio.get_running_loop()
		signals = (signal.SIGTERM, signal.SIGINT)
		for sig in signals:
			loop.add_signal_handler(
				sig,
				lambda s=sig: asyncio.create_task(self.shutdown(sig))
			)
		
		async with aiohttp.ClientSession() as session:
			try:
				self.client.session = session
				await self.client.connect()
				
				self.tasks = [
					asyncio.create_task(self.ban_monitor.torrent_monitor()),
					asyncio.create_task(self.ban_monitor.reset_tracked_torrents()),
					asyncio.create_task(self.peer_tracker.reset_tracked_peers())
				]
				
				await self.shutdown_event.wait()
				
				for task in self.tasks:
					task.cancel()
				
				await asyncio.wait(self.tasks, timeout=5)
			
			except Exception as e:
				log.critical(f"Critical error: {str(e)}")
				raise

	async def shutdown(self, sig):
		log.info(f"Received exit signal {sig.name}...")
		
		self.shutdown_event.set()
		if self.client:
			await self.client.close()
		
		log.info("Shutdown complete.")

log = logging.getLogger()
if __name__ == "__main__":
	try:
		parser = argparse.ArgumentParser(description="qbitban configuration")
		parser.add_argument("--config", type=str, required=True)
		args = parser.parse_args()
		
		log.debug("Starting application...")
		qbitban = Qbitban(args.config)
		asyncio.run(qbitban.main(), debug=False)
	
	except Exception as e:
		log.error(f"Application error: {str(e)}", exc_info=True)
		sys.exit(1)