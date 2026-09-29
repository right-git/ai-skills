import httpx
import asyncio
from bs4 import BeautifulSoup
import argparse

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"

async def search_icons(query: str, cache: bool) -> list[str]:
    url = "https://uxwing.com/"

    async with httpx.AsyncClient() as client:
        headers = {"User-Agent": USER_AGENT}
        response = await client.get(url, headers=headers, params={"s": query})
        if cache:
            with open("response.html", "w", encoding="utf-8") as f:
                f.write(response.text)
        
        response.raise_for_status()  # Raise an exception for HTTP errors
        soup = BeautifulSoup(response.text, "html.parser")
        # Extract icon URLs from the parsed HTML
        # Find all matching img tags
        images = soup.select('.post-thumbnail img')

        # Extract the 'src' attribute from each image
        icon_urls = [img['src'] for img in images if img.has_attr('src')]

    return icon_urls


async def main():
    parser = argparse.ArgumentParser(description="Search for icons on uxwing.com")
    parser.add_argument("query", type=str, help="Search query for icons")
    parser.add_argument(
        "cache",
        type=bool,
        nargs="?",
        help="Whether to cache the search response",
        default=False,
    )
    args = parser.parse_args()
    icon_urls = await search_icons(args.query, cache=args.cache)
    if icon_urls:
        print("Found icons:")
        for url in icon_urls:
            print(url)
    else:
        print("No icons found for the query.")

if __name__ == "__main__":
    asyncio.run(main())
