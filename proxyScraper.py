import argparse
import asyncio
import ipaddress
import logging
import platform
import re
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

import httpx
from bs4 import BeautifulSoup

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Known bad IP ranges to filter out (Cloudflare, major CDNs, etc.)
BAD_IP_RANGES = [
    # Cloudflare
    "173.245.48.0/20",
    "103.21.244.0/22", 
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",  # This includes our problematic IP 104.16.1.31
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    # Amazon CloudFront
    "13.32.0.0/15",
    "13.35.0.0/17",
    "18.160.0.0/15",
    "52.222.128.0/17",
    "54.182.0.0/16",
    "54.192.0.0/16",
    "54.230.0.0/16",
    "54.239.128.0/18",
    "99.86.0.0/16",
    "205.251.200.0/21",
    "216.137.32.0/19",
]

def is_bad_ip(ip: str) -> bool:
    """Check if an IP is in a known bad range (CDN, etc.) or is a reserved address."""
    try:
        ip_obj = ipaddress.ip_address(ip)
        
        # Check for reserved/special addresses
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved or ip_obj.is_multicast:
            return True
            
        # Check for specific bad addresses
        if str(ip_obj) in ["0.0.0.0", "255.255.255.255", "127.0.0.1"]:
            return True
        
        # Check against known bad ranges (CDNs)
        for cidr in BAD_IP_RANGES:
            if ip_obj in ipaddress.ip_network(cidr):
                return True
                
    except (ValueError, ipaddress.AddressValueError):
        return True  # Invalid IP format
    return False


class Scraper:
    """Base scraper class for proxy sources."""

    def __init__(self, method: str, _url: str, timeout: int = 10):
        self.method = method
        self._url = _url
        self.timeout = timeout
        self.source_name = self.__class__.__name__

    def get_url(self, **kwargs) -> str:
        """Get the formatted URL for the scraper."""
        return self._url.format(**kwargs, method=self.method)

    async def get_response(self, client: httpx.AsyncClient) -> httpx.Response:
        """Get HTTP response from the proxy source."""
        return await client.get(self.get_url(), timeout=self.timeout)

    async def handle(self, response: httpx.Response) -> str:
        """Handle the response and extract proxy data."""
        return response.text

    def filter_proxies(self, proxy_text: str) -> Tuple[Set[str], Dict[str, int]]:
        """Filter proxies and return valid ones with statistics."""
        proxies = set()
        stats = {"total": 0, "filtered_bad": 0, "filtered_invalid": 0, "valid": 0}
        
        for line in proxy_text.split('\n'):
            line = line.strip()
            if not line:
                continue
                
            stats["total"] += 1
            
            # Basic format validation
            if ':' not in line:
                stats["filtered_invalid"] += 1
                continue
                
            try:
                ip, port = line.split(':', 1)
                ip = ip.strip()
                port = port.strip()
                
                # Validate IP format
                ipaddress.ip_address(ip)
                
                # Validate port
                port_num = int(port)
                if not (1 <= port_num <= 65535):
                    stats["filtered_invalid"] += 1
                    continue
                
                # Check if it's a bad IP (CDN, etc.)
                if is_bad_ip(ip):
                    stats["filtered_bad"] += 1
                    logger.debug(f"Filtered bad IP from {self.source_name}: {ip}:{port}")
                    continue
                
                proxies.add(f"{ip}:{port}")
                stats["valid"] += 1
                
            except (ValueError, ipaddress.AddressValueError):
                stats["filtered_invalid"] += 1
                continue
        
        return proxies, stats

    async def scrape(self, client: httpx.AsyncClient) -> Tuple[List[str], Dict[str, int]]:
        """Scrape proxies from the source."""
        try:
            response = await self.get_response(client)
            response.raise_for_status()  # Raise an exception for bad status codes
            proxy_text = await self.handle(response)
            
            # Use regex to find all potential proxies
            pattern = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?")
            raw_proxies = re.findall(pattern, proxy_text)
            
            # Filter and validate proxies
            valid_proxies, stats = self.filter_proxies('\n'.join(raw_proxies))
            
            return list(valid_proxies), stats
        except Exception as e:
            logger.debug(f"Failed to scrape from {self.source_name} ({self.get_url()}): {e}")
            return [], {"total": 0, "filtered_bad": 0, "filtered_invalid": 0, "valid": 0}


# From spys.me
class SpysMeScraper(Scraper):
    """Scraper for spys.me proxy source."""

    def __init__(self, method: str):
        super().__init__(method, "https://spys.me/{mode}.txt", timeout=15)

    def get_url(self, **kwargs) -> str:
        """Get URL with appropriate mode for the proxy method."""
        mode = "proxy" if self.method == "http" else "socks" if self.method == "socks" else "unknown"
        if mode == "unknown":
            raise NotImplementedError(f"Method {self.method} not supported by SpysMeScraper")
        return super().get_url(mode=mode, **kwargs)


# From proxyscrape.com
class ProxyScrapeScraper(Scraper):
    """Scraper for proxyscrape.com API."""

    def __init__(self, method: str, timeout: int = 1000, country: str = "All"):
        self.api_timeout = timeout  # Renamed to avoid confusion with HTTP timeout
        self.country = country
        super().__init__(method,
                         "https://api.proxyscrape.com/?request=getproxies"
                         "&proxytype={method}"
                         "&timeout={api_timeout}"
                         "&country={country}", 
                         timeout=20)  # HTTP timeout

    def get_url(self, **kwargs) -> str:
        """Get URL with API parameters."""
        return super().get_url(api_timeout=self.api_timeout, country=self.country, **kwargs)

# From geonode.com - A little dirty, grab http(s) and socks but use just for socks
class GeoNodeScraper(Scraper):
    """Scraper for geonode.com proxy API."""

    def __init__(self, method: str, limit: str = "500", page: str = "1", 
                 sort_by: str = "lastChecked", sort_type: str = "desc"):
        self.limit = limit
        self.page = page
        self.sort_by = sort_by
        self.sort_type = sort_type
        super().__init__(method,
                         "https://proxylist.geonode.com/api/proxy-list?"
                         "&limit={limit}"
                         "&page={page}"
                         "&sort_by={sort_by}"
                         "&sort_type={sort_type}",
                         timeout=15)

    def get_url(self, **kwargs) -> str:
        """Get URL with API parameters."""
        return super().get_url(limit=self.limit, page=self.page, 
                               sort_by=self.sort_by, sort_type=self.sort_type, **kwargs)


# From proxy-list.download
class ProxyListDownloadScraper(Scraper):
    """Scraper for proxy-list.download API."""

    def __init__(self, method: str, anon: str):
        self.anon = anon
        super().__init__(method, "https://www.proxy-list.download/api/v1/get?type={method}&anon={anon}", timeout=15)

    def get_url(self, **kwargs) -> str:
        """Get URL with anonymity level parameter."""
        return super().get_url(anon=self.anon, **kwargs)


# For websites using table in html
class GeneralTableScraper(Scraper):
    """Scraper for websites that use HTML tables to display proxies."""

    async def handle(self, response: httpx.Response) -> str:
        """Parse HTML table to extract proxies."""
        try:
            soup = BeautifulSoup(response.text, "html.parser")
            proxies: Set[str] = set()
            table = soup.find("table", attrs={"class": "table table-striped table-bordered"})
            
            if table is None:
                logger.debug("No table found with expected class")
                return ""
                
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) >= 2:
                    ip = cells[0].get_text(strip=True).replace("&nbsp;", "")
                    port = cells[1].get_text(strip=True).replace("&nbsp;", "")
                    if ip and port:
                        proxies.add(f"{ip}:{port}")
            
            return "\n".join(proxies)
        except Exception as e:
            logger.debug(f"Error parsing HTML table: {e}")
            return ""


# For websites using div in html
class GeneralDivScraper(Scraper):
    """Scraper for websites that use HTML divs to display proxies."""

    async def handle(self, response: httpx.Response) -> str:
        """Parse HTML divs to extract proxies."""
        try:
            soup = BeautifulSoup(response.text, "html.parser")
            proxies: Set[str] = set()
            container = soup.find("div", attrs={"class": "list"})
            
            if container is None:
                logger.debug("No div found with class 'list'")
                return ""
                
            for row in container.find_all("div"):
                cells = row.find_all("div", attrs={"class": "td"})
                if len(cells) >= 2:
                    ip = cells[0].get_text(strip=True)
                    port = cells[1].get_text(strip=True)
                    if ip and port:
                        proxies.add(f"{ip}:{port}")
            
            return "\n".join(proxies)
        except Exception as e:
            logger.debug(f"Error parsing HTML divs: {e}")
            return ""
    
# For scraping live proxylist from github
class GitHubScraper(Scraper):
    """Scraper for GitHub raw proxy lists."""
        
    async def handle(self, response: httpx.Response) -> str:
        """Parse GitHub raw proxy list format."""
        try:
            temp_proxies = response.text.strip().split("\n")
            proxies: Set[str] = set()
            
            for proxy_line in temp_proxies:
                proxy_line = proxy_line.strip()
                if not proxy_line:
                    continue
                    
                # Handle different formats: "type://ip:port" or just "ip:port"
                if self.method in proxy_line:
                    # Extract IP:port from lines like "http://1.2.3.4:8080"
                    if "//" in proxy_line:
                        proxy = proxy_line.split("//")[-1]
                    else:
                        proxy = proxy_line
                    
                    # Validate IP:port format
                    if re.match(r"\d{1,3}(?:\.\d{1,3}){3}:\d{1,5}", proxy):
                        proxies.add(proxy)

            return "\n".join(proxies)
        except Exception as e:
            logger.debug(f"Error parsing GitHub proxy list: {e}")
            return ""

# For scraping from proxy list APIs with JSON response
class ProxyListApiScraper(Scraper):
    """Scraper for APIs that return JSON proxy lists."""
    
    def _extract_proxy_from_item(self, item: dict) -> Optional[str]:
        """Extract proxy string from a single item."""
        if not isinstance(item, dict):
            return None
            
        ip = item.get('ip')
        port = item.get('port')
        if ip and port:
            return f"{ip}:{port}"
        return None
    
    def _process_list_data(self, data: list) -> Set[str]:
        """Process list-type JSON data."""
        proxies = set()
        for item in data:
            proxy = self._extract_proxy_from_item(item)
            if proxy:
                proxies.add(proxy)
        return proxies
    
    def _process_dict_data(self, data: dict) -> Set[str]:
        """Process dict-type JSON data."""
        proxies = set()
        if 'data' in data and isinstance(data['data'], list):
            for item in data['data']:
                proxy = self._extract_proxy_from_item(item)
                if proxy:
                    proxies.add(proxy)
        return proxies

    async def handle(self, response: httpx.Response) -> str:
        """Parse JSON API response for proxies."""
        try:
            data = response.json()
            proxies: Set[str] = set()
            
            # Handle different JSON structures
            if isinstance(data, list):
                proxies = self._process_list_data(data)
            elif isinstance(data, dict):
                proxies = self._process_dict_data(data)
                            
            return "\n".join(proxies)
        except Exception as e:
            logger.debug(f"Error parsing JSON API response: {e}")
            return ""

# For scraping from plain text sources
class PlainTextScraper(Scraper):
    """Scraper for plain text proxy lists."""
    
    async def handle(self, response: httpx.Response) -> str:
        """Parse plain text proxy list."""
        try:
            proxies: Set[str] = set()
            lines = response.text.strip().split('\n')
            
            for line in lines:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                    
                # Look for IP:port pattern
                if re.match(r"\d{1,3}(?:\.\d{1,3}){3}:\d{1,5}", line):
                    proxies.add(line)
                    
            return "\n".join(proxies)
        except Exception as e:
            logger.debug(f"Error parsing plain text proxy list: {e}")
            return ""


# Improved scrapers list with better organization
scrapers = [
    # Direct API scrapers
    SpysMeScraper("http"),
    SpysMeScraper("socks"),
    ProxyScrapeScraper("http"),
    ProxyScrapeScraper("socks4"),
    ProxyScrapeScraper("socks5"),
    GeoNodeScraper("socks"),
    
    # Download API scrapers
    ProxyListDownloadScraper("https", "elite"),
    ProxyListDownloadScraper("http", "elite"),
    ProxyListDownloadScraper("http", "transparent"),
    ProxyListDownloadScraper("http", "anonymous"),
    
    # HTML table scrapers
    GeneralTableScraper("https", "http://sslproxies.org"),
    GeneralTableScraper("http", "http://free-proxy-list.net"),
    GeneralTableScraper("http", "http://us-proxy.org"),
    GeneralTableScraper("socks", "http://socks-proxy.net"),
    
    # HTML div scrapers
    GeneralDivScraper("http", "https://freeproxy.lunaproxy.com/"),
    
    # GitHub raw list scrapers (established sources)
    GitHubScraper("http", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
    GitHubScraper("socks4", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
    GitHubScraper("socks5", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
    GitHubScraper("http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt"),
    GitHubScraper("socks", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt"),
    GitHubScraper("https", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt"),
    GitHubScraper("http", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt"),
    GitHubScraper("socks4", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks4.txt"),
    GitHubScraper("socks5", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt"),
    
    # Additional GitHub sources
    GitHubScraper("http", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    GitHubScraper("socks4", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt"),
    GitHubScraper("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    GitHubScraper("http", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt"),
    GitHubScraper("https", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt"),
    GitHubScraper("socks4", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt"),
    GitHubScraper("socks5", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt"),
    GitHubScraper("http", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt"),
    GitHubScraper("https", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt"),
    GitHubScraper("socks4", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt"),
    GitHubScraper("socks5", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt"),
    GitHubScraper("http", "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"),
    GitHubScraper("http", "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt"),
    GitHubScraper("http", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
    GitHubScraper("socks4", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt"),
    GitHubScraper("socks5", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt"),
    GitHubScraper("http", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"),
    GitHubScraper("https", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt"),
    GitHubScraper("socks4", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt"),
    GitHubScraper("socks5", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt"),
    
    # Plain text sources
    PlainTextScraper("http", "https://www.proxyscan.io/download?type=http"),
    PlainTextScraper("socks4", "https://www.proxyscan.io/download?type=socks4"),
    PlainTextScraper("socks5", "https://www.proxyscan.io/download?type=socks5"),
    PlainTextScraper("http", "https://raw.githubusercontent.com/almroot/proxylist/master/list.txt"),
    PlainTextScraper("http", "https://raw.githubusercontent.com/aslisk/proxyhttps/main/https.txt"),
    PlainTextScraper("http", "https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/http.txt"),
    
    # Additional table scrapers
    GeneralTableScraper("http", "https://proxyspace.pro/http.txt"),
    GeneralTableScraper("socks4", "https://proxyspace.pro/socks4.txt"),
    GeneralTableScraper("socks5", "https://proxyspace.pro/socks5.txt"),
    
    # API-based scrapers
    ProxyListApiScraper("http", "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=http"),
    ProxyListApiScraper("socks5", "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=socks5"),
]


def verbose_print(verbose: bool, message: str) -> None:
    """Print message if verbose mode is enabled."""
    if verbose:
        print(message)


def _determine_scraping_methods(method: str) -> List[str]:
    """Determine which methods to scrape based on input."""
    methods = [method]
    if method == "socks":
        methods.extend(["socks4", "socks5"])
    return methods

def _get_scrapers_for_methods(methods: List[str]) -> List:
    """Get scrapers that match the specified methods."""
    proxy_scrapers = [s for s in scrapers if s.method in methods]
    if not proxy_scrapers:
        raise ValueError(f"Methods '{methods}' not supported")
    return proxy_scrapers

def _create_http_client_config() -> Dict:
    """Create HTTP client configuration."""
    return {
        "follow_redirects": True,
        "timeout": 30.0,
        "limits": httpx.Limits(max_keepalive_connections=20, max_connections=100),
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        },
    }

def _print_source_statistics(verbose: bool, source_stats: Dict) -> None:
    """Print source statistics if verbose mode is enabled."""
    if not verbose:
        return
        
    print("\n📊 Source Statistics:")
    print("-" * 50)
    total_bad_filtered = 0
    total_invalid_filtered = 0
    for source, stats in source_stats.items():
        print(f"{source}: {stats['valid']} valid, {stats['filtered_bad']} bad IPs, {stats['filtered_invalid']} invalid")
        total_bad_filtered += stats['filtered_bad']
        total_invalid_filtered += stats['filtered_invalid']
    print(f"\nTotal filtered: {total_bad_filtered} bad IPs (CDN/etc), {total_invalid_filtered} invalid format")

async def scrape(method: str, output: str, verbose: bool) -> None:
    """
    Main scraping function that coordinates all scrapers.
    
    Args:
        method: Proxy type to scrape (http, https, socks, socks4, socks5)
        output: Output file path
        verbose: Enable verbose logging
    """
    start_time = time.time()
    
    # Setup scraping parameters
    methods = _determine_scraping_methods(method)
    proxy_scrapers = _get_scrapers_for_methods(methods)
    client_config = _create_http_client_config()
    
    verbose_print(verbose, f"Scraping proxies using {len(proxy_scrapers)} sources...")
    all_proxies: List[str] = []
    source_stats: Dict[str, Dict[str, int]] = {}

    async def scrape_source(scraper, client) -> None:
        """Scrape from a single source."""
        try:
            verbose_print(verbose, f"Scraping from {scraper.get_url()}...")
            proxies, stats = await scraper.scrape(client)
            all_proxies.extend(proxies)
            source_stats[scraper.source_name] = stats
            verbose_print(verbose, f"Found {len(proxies)} valid proxies from {scraper.source_name} ({stats['filtered_bad']} bad IPs filtered, {stats['filtered_invalid']} invalid filtered)")
        except Exception as e:
            logger.debug(f"Failed to scrape from {scraper.source_name}: {e}")
            source_stats[scraper.source_name] = {"total": 0, "filtered_bad": 0, "filtered_invalid": 0, "valid": 0}

    # Execute all scrapers concurrently
    async with httpx.AsyncClient(**client_config) as client:
        tasks = [scrape_source(scraper, client) for scraper in proxy_scrapers]
        await asyncio.gather(*tasks, return_exceptions=True)

    # Process results
    unique_proxies: Set[str] = set(all_proxies)
    _print_source_statistics(verbose, source_stats)

    # Write results to file
    verbose_print(verbose, f"Writing {len(unique_proxies)} unique proxies to {output}...")
    try:
        with open(output, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(unique_proxies)) + "\n")
    except IOError as e:
        logger.error(f"Failed to write to output file {output}: {e}")
        raise

    elapsed_time = time.time() - start_time
    verbose_print(verbose, f"Scraping completed in {elapsed_time:.2f} seconds")
    verbose_print(verbose, f"Found {len(unique_proxies)} unique valid proxies")

def _setup_argument_parser():
    """Set up and return the argument parser."""
    parser = argparse.ArgumentParser(
        description="Scrape proxies from multiple sources",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -p http -v                    # Scrape HTTP proxies with verbose output
  %(prog)s -p socks -o socks.txt         # Scrape SOCKS proxies to custom file
  %(prog)s -p https --verbose            # Scrape HTTPS proxies with verbose output
        """,
    )
    
    supported_methods = sorted(set(s.method for s in scrapers))
    
    parser.add_argument(
        "-p", "--proxy",
        required=True,
        choices=supported_methods,
        help=f"Proxy type to scrape. Supported types: {', '.join(supported_methods)}",
    )
    parser.add_argument(
        "-o", "--output",
        default="output.txt",
        help="Output file name to save proxies (default: %(default)s)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose output",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    
    return parser

def _configure_logging(args):
    """Configure logging based on command line arguments."""
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.verbose:
        logging.getLogger().setLevel(logging.INFO)
    else:
        logging.getLogger().setLevel(logging.WARNING)

def _run_scraping(args):
    """Run the scraping process with appropriate event loop handling."""
    if sys.version_info >= (3, 7):
        if platform.system() == 'Windows':
            # Windows-specific asyncio policy for better compatibility
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        asyncio.run(scrape(args.proxy, args.output, args.verbose))
    else:
        # Fallback for Python < 3.7
        loop = asyncio.get_event_loop()
        try:
            loop.run_until_complete(scrape(args.proxy, args.output, args.verbose))
        finally:
            loop.close()

def main() -> None:
    """Main entry point for the proxy scraper."""
    parser = _setup_argument_parser()
    args = parser.parse_args()
    
    _configure_logging(args)

    try:
        _run_scraping(args)
    except KeyboardInterrupt:
        print("\nScraping interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Scraping failed: {e}")
        if args.debug:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
