# File: claudesidian/web/scraper.py

"""
Web scraping implementation using Puppeteer.
Handles browser automation, content extraction, and anti-detection measures.
Includes smart URL normalization and inference.
"""

import asyncio
import random
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path
import logging
import json
from datetime import datetime
import re
import aiohttp
from urllib.parse import urljoin, urlparse

from . import (
    WebContent, ScrapingConfig, ImageHandling, ContentPriority,
    ScrapingError, RateLimitError
)

logger = logging.getLogger(__name__)

class WebScraper:
    """
    Advanced web scraper using Puppeteer with anti-detection, resilience features,
    and smart URL handling.
    """
    
    # URL handling patterns
    COMMON_PROTOCOLS = ['https://', 'http://']
    COMMON_PREFIXES = ['www.', '']
    COMMON_SUFFIXES = [
        '.com', '.ai', '.org', '.net', '.io', '.co', 
        '.edu', '.gov', '.uk', '.de', '.cn', '.jp'
    ]
    
    # Default configurations
    DEFAULT_CONFIG = {
        'max_wait_time': 30,          # 30 seconds max wait
        'screenshot_enabled': True,
        'js_enabled': True,
        'timeout': 60000,             # 60 second timeout
        'max_redirects': 5,
        'viewport': {
            'width': 1920,
            'height': 1080
        },
        'user_agents': [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0'
        ]
    }
    
    # Content selectors
    CONTENT_SELECTORS = {
        'article': ['article', 'main', '.article', '.post', '.content', '#content'],
        'title': ['h1', '.title', '.post-title', 'article h1'],
        'date': [
            'time', 
            '[datetime]', 
            '.date', 
            '.post-date',
            'meta[property="article:published_time"]'
        ],
        'author': [
            '[rel="author"]',
            '.author',
            '.byline',
            'meta[name="author"]'
        ]
    }

    def __init__(self, 
                 config: Optional[ScrapingConfig] = None,
                 vault_path: Optional[Path] = None):
        """Initialize the web scraper."""
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self.vault_path = vault_path
        self._setup_puppeteer()

    async def normalize_url(self, url: str) -> str:
        """
        Normalize and infer complete URL from partial input.
        
        Args:
            url: Partial or complete URL
            
        Returns:
            Complete, normalized URL
        """
        url = url.strip().lower()
        
        # If it's already a complete URL, just normalize it
        if url.startswith(('http://', 'https://')):
            return url
            
        # Remove any protocol if accidentally included
        url = re.sub(r'^.*://', '', url)
        
        # Try different combinations of protocols, prefixes, and suffixes
        candidates = []
        is_ip = bool(re.match(r'^\d{1,3}(\.\d{1,3}){3}$', url))
        
        if not is_ip:
            # If no suffix provided, try common ones
            if not any(url.endswith(suffix) for suffix in self.COMMON_SUFFIXES):
                base_candidates = []
                for suffix in self.COMMON_SUFFIXES:
                    if url.endswith(suffix):
                        base_candidates.append(url)
                    else:
                        base_candidates.append(f"{url}{suffix}")
            else:
                base_candidates = [url]
                
            # Try different prefix combinations
            for base in base_candidates:
                if not any(base.startswith(prefix) for prefix in self.COMMON_PREFIXES):
                    for prefix in self.COMMON_PREFIXES:
                        candidates.append(f"{prefix}{base}")
                else:
                    candidates.append(base)
        else:
            candidates = [url]
            
        # Try each candidate with different protocols
        final_candidates = []
        for candidate in candidates:
            for protocol in self.COMMON_PROTOCOLS:
                final_candidates.append(f"{protocol}{candidate}")
                
        # Try each candidate until one works
        async with aiohttp.ClientSession() as session:
            for candidate in final_candidates:
                try:
                    async with session.head(candidate, allow_redirects=True) as response:
                        if response.status < 400:
                            return str(response.url)
                except:
                    continue
                    
        # If no candidates work, use HTTPS with www prefix as fallback
        return f"https://www.{url}" if not is_ip else f"https://{url}"

    async def scrape(self, url: str) -> WebContent:
        """
        Scrape content from a URL with automatic URL normalization.
        
        Args:
            url: Full or partial URL to scrape
            
        Returns:
            WebContent object
        """
        # Normalize URL first
        normalized_url = await self.normalize_url(url)
        
        try:
            # Create new page with random user agent
            page = await self.browser.newPage()
            await self._setup_page(page)
            
            # Navigate with extended timeout
            response = await page.goto(normalized_url, {
                'waitUntil': 'networkidle0',
                'timeout': self.config['timeout']
            })
            
            if not response:
                # Try alternative URL formations if initial one fails
                alternatives = []
                parsed = urlparse(normalized_url)
                if not parsed.path or parsed.path == '/':
                    alternatives.extend([
                        f"{normalized_url}/index.html",
                        f"{normalized_url}/home",
                        f"{normalized_url}/main"
                    ])
                
                for alt_url in alternatives:
                    try:
                        response = await page.goto(alt_url, {
                            'waitUntil': 'networkidle0',
                            'timeout': self.config['timeout']
                        })
                        if response:
                            break
                    except:
                        continue
                        
                if not response:
                    raise ScrapingError("Failed to load page", normalized_url)
                    
            status = response.status()
            if status >= 400:
                raise ScrapingError(f"HTTP {status}", normalized_url, status)
                
            # Handle common anti-bot measures
            await self._handle_anti_bot(page)
            
            # Extract content
            content = WebContent(normalized_url)
            
            # Get metadata
            content.metadata = await self._extract_metadata(page)
            
            # Get main content based on priority
            content.content = await self._extract_content(page)
            
            # Handle images if enabled
            if self.config.get('image_handling') != ImageHandling.IGNORE:
                content.images = await self._handle_images(page)
                
            # Extract links
            content.links = await self._extract_links(page)
            
            # Take screenshots if enabled
            if self.config.get('screenshot_enabled'):
                content.screenshots = await self._take_screenshots(page)
                
            # Clean up
            await page.close()
            
            return content
            
        except Exception as e:
            if isinstance(e, puppeteer.errors.TimeoutError):
                raise ScrapingError("Page load timeout", normalized_url)
            raise

    async def _setup_puppeteer(self):
        """Initialize Puppeteer with stealth settings."""
        self.browser = await puppeteer.launch({
            'headless': True,
            'args': [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas',
                '--disable-gpu',
                '--disable-notifications',
                '--disable-extensions',
                '--disable-infobars',
                '--window-size=1920,1080',
                '--ignore-certificate-errors',
                '--disable-blink-features=AutomationControlled',
            ]
        })

    async def _setup_page(self, page) -> None:
        """Configure page with anti-detection measures."""
        # Set random user agent
        user_agent = random.choice(self.config['user_agents'])
        await page.setUserAgent(user_agent)
        
        # Set viewport
        await page.setViewport(self.config['viewport'])
        
        # Set extra headers
        await page.setExtraHTTPHeaders({
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'DNT': '1',
            'Upgrade-Insecure-Requests': '1'
        })
        
        # Inject stealth scripts
        await self._inject_stealth_scripts(page)

    async def _inject_stealth_scripts(self, page) -> None:
        """Inject scripts to avoid detection."""
        await page.evaluateOnNewDocument('''
            // Override web APIs to avoid detection
            Object.defineProperty(navigator, 'webdriver', {
                get: () => false,
            });
            
            // Override Chrome
            window.chrome = {
                runtime: {},
            };
            
            // Override permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
            );
            
            // Override plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => [
                    {
                        0: {type: "application/x-google-chrome-pdf"},
                        description: "Portable Document Format",
                        filename: "internal-pdf-viewer",
                        length: 1,
                        name: "Chrome PDF Plugin"
                    }
                ],
            });
            
            // Override languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });
        ''')

    async def _handle_anti_bot(self, page) -> None:
        """Handle common anti-bot measures."""
        # Wait for page to be truly ready
        await page.waitForFunction(
            'document.readyState === "complete" && !document.querySelector("body.loading")',
            {'timeout': self.config['timeout']}
        )
        
        # Scroll page to simulate human behavior
        await self._simulate_scrolling(page)
        
        # Handle common cookie consent popups
        await self._handle_cookie_popups(page)
        
        # Check for CAPTCHA and other challenges
        await self._handle_challenges(page)

    async def _simulate_scrolling(self, page) -> None:
        """Simulate human-like scrolling behavior."""
        await page.evaluate('''
            () => {
                const scroll = () => {
                    const height = document.body.scrollHeight;
                    const duration = 10000; // 10 seconds
                    const start = performance.now();
                    
                    const step = (timestamp) => {
                        const elapsed = timestamp - start;
                        const progress = Math.min(elapsed / duration, 1);
                        
                        window.scrollTo(0, height * progress);
                        
                        if (progress < 1) {
                            requestAnimationFrame(step);
                        }
                    };
                    
                    requestAnimationFrame(step);
                };
                scroll();
            }
        ''')
        
        # Wait for scrolling to complete
        await asyncio.sleep(2)

    async def _handle_challenges(self, page) -> None:
        """Handle CAPTCHA and other security challenges."""
        # Check for common CAPTCHA services
        captcha_detected = await page.evaluate('''
            () => {
                return !!(
                    document.querySelector('.g-recaptcha') ||
                    document.querySelector('#captcha') ||
                    document.querySelector('[class*="captcha"]') ||
                    document.querySelector('iframe[src*="captcha"]')
                );
            }
        ''')
        
        if captcha_detected:
            raise ScrapingError("CAPTCHA detected", page.url)
            
        # Check for Cloudflare challenge
        if await page.evaluate("() => !!document.querySelector('#cf-wrapper')"):
            raise ScrapingError("Cloudflare protection detected", page.url)

    async def close(self) -> None:
        """Clean up browser instance."""
        if self.browser:
            await self.browser.close()
            self.browser = None