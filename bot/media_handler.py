"""Smart Media Handler — Image scoring and album support for Asya Bot.

FEATURES:
- Image quality scoring (resolution, size, aspect ratio)
- Bad image filtering (banners, logos, icons, tracking pixels)
- Album support (media groups up to 10 photos)
- Partner post protection (only use partner-provided images)
- Telegram limit compliance (1024 caption, 4096 message)
"""

from bot import config
import logging
import re
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
import httpx

logger = logging.getLogger("asya.media_handler")


class ImageQuality(Enum):
    """Image quality classification"""
    EXCELLENT = "excellent"  # Perfect for albums (high-res, good aspect)
    GOOD = "good"            # Suitable for single posts
    POOR = "poor"            # Acceptable if no better option
    REJECT = "reject"        # Trash — banners, icons, tracking pixels


@dataclass
class ScoredImage:
    """Image with quality score and metadata"""
    url: str
    quality: ImageQuality
    score: int  # 0-100
    width: Optional[int] = None
    height: Optional[int] = None
    size_kb: Optional[int] = None
    source: str = "unknown"  # original, web_search, ai_generated


class MediaHandler:
    """Smart media handler with quality scoring and album support"""
    
    def __init__(self):
        # Minimum requirements — relaxed to capture more article photos
        self.MIN_WIDTH = 200
        self.MIN_HEIGHT = 150
        self.MIN_SIZE_KB = 15
        self.MAX_SIZE_KB = 5000  # 5MB max for fast loading
        
        # Bad keywords in URL (reject these images)
        self.BAD_KEYWORDS = [
            'banner', 'ad', 'advertisement', 'logo', 'icon', 'spacer',
            '1x1', 'pixel', 'tracking', 'analytics', 'button',
            'social', 'share', 'widget', 'sidebar', 'footer',
            'newsletter', 'subscribe', 'popup', 'avatar', 'favicon',
            'badge', 'tracking-pixel', 'beacon', 'webstat'
        ]
        
        # Good keywords in URL (prefer these images)
        self.GOOD_KEYWORDS = [
            'press', 'official', 'gallery', 'hero', 'main',
            'exterior', 'interior', 'detail', 'review', 'news',
            'media', 'cdn', 'images', 'uploads', 'wp-content'
        ]
        
        # Trusted domains for images
        self.TRUSTED_DOMAINS = [
            'cdn', 'media', 'images', 'static', 'img',
            'cdn-', 'media-', 'img-', 'static-'
        ]
    
    async def process_media_for_post(
        self, 
        article_data: Dict, 
        is_partner: bool = False,
        tone: str = "routine"
    ) -> Dict:
        """
        Process media for a post with smart scoring.
        
        Returns: {
            "type": "single" | "album" | "text_only",
            "media": [ScoredImage, ...],
            "source": "partner" | "original" | "web_search" | "ai_generated"
        }
        """
        logger.info(f"🖼️ Processing media (partner={is_partner}, tone={tone})")
        
        # Collect candidate images
        candidates = self._collect_candidate_images(article_data)
        
        if not candidates:
            logger.info("❌ No images found")
            return {"type": "text_only", "media": [], "source": "none"}
        
        # Score each image
        scored_images = []
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            for url in candidates[:10]:  # Limit to 10 candidates
                scored = await self._score_image(client, url, article_data)
                if scored and scored.quality != ImageQuality.REJECT:
                    scored_images.append(scored)
        
        if not scored_images:
            logger.info("❌ All images rejected by scoring")
            return {"type": "text_only", "media": [], "source": "none"}
        
        # Sort by score (highest first)
        scored_images.sort(key=lambda x: x.score, reverse=True)
        
        # PARTNER POSTS: Only use partner-provided images (STRICT)
        if is_partner:
            logger.info(f"✅ Partner post: using only partner image")
            return {
                "type": "single",
                "media": [scored_images[0]],
                "source": "partner"
            }
        
        # SERIOUS NEWS: Single photo only (no albums for accidents/recalls)
        if tone == "serious":
            logger.info(f"✅ Serious news: single photo")
            return {
                "type": "single",
                "media": [scored_images[0]],
                "source": scored_images[0].source
            }
        
        # REGULAR NEWS: Try album with ALL non-rejected images (up to 10)
        # Prioritize EXCELLENT/GOOD, then add POOR if we have room
        album_candidates = [
            img for img in scored_images 
            if img.quality in [ImageQuality.EXCELLENT, ImageQuality.GOOD]
        ]
        
        # If we have fewer than 2 excellent/good images, include POOR ones too
        if len(album_candidates) < 2:
            poor_candidates = [
                img for img in scored_images 
                if img.quality == ImageQuality.POOR
            ]
            album_candidates.extend(poor_candidates)
            album_candidates.sort(key=lambda x: x.score, reverse=True)
        
        MAX_ALBUM_SIZE = 10  # Telegram allows up to 10 photos per media group
        
        if len(album_candidates) >= 2:
            # Create album
            album = album_candidates[:min(MAX_ALBUM_SIZE, len(album_candidates))]
            logger.info(f"✅ Creating album with {len(album)} photos")
            return {
                "type": "album",
                "media": album,
                "source": album[0].source
            }
        else:
            # Single photo
            logger.info(f"✅ Single photo: {scored_images[0].url[:80]}")
            return {
                "type": "single",
                "media": [scored_images[0]],
                "source": scored_images[0].source
            }
    
    def _collect_candidate_images(self, article_data: Dict) -> List[str]:
        """Collect all possible image URLs from article data"""
        images = []
        seen = set()
        
        def add_url(url: str):
            if url and url not in seen and len(url) > 30:
                # Skip data URIs and very short URLs
                if not url.startswith('data:'):
                    seen.add(url)
                    images.append(url)
        
        # 1. Original image from RSS
        if article_data.get("image"):
            add_url(article_data["image"])
        
        # 2. Image URLs list
        if article_data.get("image_urls"):
            for url in article_data["image_urls"]:
                add_url(url)
        
        # 3. Thumbnail
        if article_data.get("thumbnail"):
            add_url(article_data["thumbnail"])
        
        # 4. Featured image
        if article_data.get("featured_image"):
            add_url(article_data["featured_image"])
        
        # 5. Extract from HTML content
        if article_data.get("content"):
            html_images = self._extract_images_from_html(article_data["content"])
            for url in html_images:
                add_url(url)
        
        logger.debug(f"Collected {len(images)} candidate images")
        return images
    
    def _extract_images_from_html(self, html: str) -> List[str]:
        """Extract image URLs from HTML content"""
        # Find all img src attributes
        pattern = r'<img[^>]+src=["\']([^"\']+)["\']'
        matches = re.findall(pattern, html, re.IGNORECASE)
        
        # Filter valid URLs
        valid = []
        for url in matches:
            if url.startswith(('http://', 'https://')):
                valid.append(url)
        
        return valid[:10]  # Max 10 from HTML
    
    async def _score_image(
        self, 
        client: httpx.AsyncClient, 
        url: str, 
        article_data: Dict
    ) -> Optional[ScoredImage]:
        """Score image quality (0-100)"""
        try:
            url_lower = url.lower()
            
            # Quick reject: bad keywords in URL
            for bad_kw in self.BAD_KEYWORDS:
                if bad_kw in url_lower:
                    logger.debug(f"❌ Rejected by URL keyword: {bad_kw}")
                    return ScoredImage(url=url, quality=ImageQuality.REJECT, score=0)
            
            # Try to get image info via HEAD request
            try:
                resp = await client.head(url)
                if resp.status_code != 200:
                    return None

                content_type = resp.headers.get('content-type', '')
                if not content_type.startswith('image/'):
                    return None

                content_length = int(resp.headers.get('content-length', 0))
                size_kb = content_length // 1024

                # Size check
                if size_kb > 0 and size_kb < self.MIN_SIZE_KB:
                    logger.debug(f"Too small: {size_kb}KB")
                    return ScoredImage(url=url, quality=ImageQuality.REJECT, score=0)

                if size_kb > self.MAX_SIZE_KB:
                    logger.debug(f"Too large: {size_kb}KB")
                    # Still allow, but lower score
            except Exception as e:
                logger.debug(f"HEAD request failed: {e}")
                size_kb = 0
            
            # Calculate score
            score = self._calculate_score(url, size_kb, article_data)
            quality = self._score_to_quality(score)
            
            return ScoredImage(
                url=url,
                quality=quality,
                score=score,
                size_kb=size_kb,
                source="original"
            )
            
        except Exception as e:
            logger.error(f"Error scoring image {url[:80]}: {e}")
            return None
    
    def _calculate_score(self, url: str, size_kb: int, article_data: Dict) -> int:
        """Calculate image quality score (0-100)"""
        score = 50  # Base score
        
        url_lower = url.lower()
        
        # Bonus for good keywords
        for good_kw in self.GOOD_KEYWORDS:
            if good_kw in url_lower:
                score += 10
        
        # Bonus for trusted domains
        for domain in self.TRUSTED_DOMAINS:
            if domain in url_lower:
                score += 5
                break
        
        # Bonus for optimal file size
        if 200 <= size_kb <= 2000:
            score += 20  # Optimal
        elif 100 <= size_kb <= 3000:
            score += 10  # Good
        elif size_kb > 0:
            score += 0   # Unknown or bad
        
        # Bonus for image extensions
        if any(ext in url_lower for ext in ['.jpg', '.jpeg', '.png', '.webp']):
            score += 5
        
        # Penalty for suspicious patterns
        if 'thumb' in url_lower or 'preview' in url_lower:
            score -= 10
        
        return max(0, min(100, score))
    
    def _score_to_quality(self, score: int) -> ImageQuality:
        """Convert score to quality enum"""
        if score >= 80:
            return ImageQuality.EXCELLENT
        elif score >= 60:
            return ImageQuality.GOOD
        elif score >= 40:
            return ImageQuality.POOR
        else:
            return ImageQuality.REJECT
    
    def prepare_media_group(self, images: List[ScoredImage], caption: str) -> List[Dict]:
        """Prepare media group for Telegram send_media_group"""
        from aiogram.types import InputMediaPhoto
        from aiogram.types import FSInputFile
        
        media_group = []
        
        # Smart caption truncation — avoid mid-word cuts
        safe_caption = self._safe_truncate_caption(caption)
        
        for i, img in enumerate(images[:10]):  # Telegram limit: 10
            # For first image, add caption (limited to 1024 chars)
            if i == 0:
                media = InputMediaPhoto(
                    media=img.url,
                    caption=safe_caption,
                    parse_mode="HTML"
                )
            else:
                media = InputMediaPhoto(media=img.url)
            
            media_group.append(media)
        
        return media_group
    
    def prepare_single_photo(self, image: ScoredImage, caption: str) -> Dict:
        """Prepare single photo for Telegram send_photo"""
        safe_caption = self._safe_truncate_caption(caption)
        
        return {
            "photo": image.url,
            "caption": safe_caption,
            "parse_mode": "HTML"
        }

    def _safe_truncate_caption(self, caption: str) -> str:
        """Smart caption truncation — never cut mid-word or mid-sentence.
        
        If caption exceeds Telegram limit, finds a natural break point
        (sentence end, paragraph, newline, or word boundary) instead of
        crude character-level truncation.
        """
        limit = config.TELEGRAM_CAPTION_LIMIT
        if len(caption) <= limit:
            return caption
        
        target = limit - 3  # Room for "..."
        if target < 50:
            return caption[:target] + "..."
        
        search_zone = caption[:target + 1]
        
        # 1. Paragraph break
        last_para = search_zone.rfind("\n\n")
        if last_para > target * 0.5:
            return caption[:last_para].rstrip() + "..."
        
        # 2. Sentence end
        for end_char in ['. ', '! ', '? ', '… ']:
            pos = search_zone.rfind(end_char)
            if pos > target * 0.5:
                return caption[:pos + 1].rstrip() + "..."
        
        # 3. Newline
        last_newline = search_zone.rfind("\n")
        if last_newline > target * 0.5:
            return caption[:last_newline].rstrip() + "..."
        
        # 4. Space (avoid mid-word)
        last_space = search_zone.rfind(" ")
        if last_space > target * 0.5:
            return caption[:last_space].rstrip() + "..."
        
        # 5. Hard cut — very last resort
        return caption[:target].rstrip() + "..."


# Global instance
media_handler = MediaHandler()
