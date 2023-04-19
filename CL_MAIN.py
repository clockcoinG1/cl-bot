import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Protocol, Set

from typing_extensions import runtime_checkable

from craigslist import (
		CraigslistForSale,
)
from utils import get_url


@runtime_checkable
class Searchable(Protocol):
		def search(self, query: "ClaireQuery") -> List["ClaireListing"]:
				pass


@dataclass(slots=False)
class ClaireQuery:
		owner_id: int
		zip_code: int
		state: str
		site: str
		lat: float
		lon: float
		keywords: str
		budget: int = 1000
		distance: int = 30
		category: str = "sss"
		has_image: bool = False
		spam_probability: int = 80
		sent_listings: Set[str] = field(default_factory=set)

		def to_db(self) -> dict:
				"""Converts Claire Query to JSON for DB"""
				final_dic = {}
				for attr in self.__slots__:
						if attr not in ["sent_listings"]:
								final_dic[attr] = self.__getattribute__(attr)
				return final_dic

		def search(self) -> List["ClaireListing"]:
				"""Uses the query to search Craigslist.
				Returns a list of ALL matching posts"""
				listings: List["ClaireListing"] = []
				for source in Sources:
						if source.is_valid():
								source_listings = source.value.search(query=self)
								listings += source_listings
				return listings

		def filter_listings(self, listings: List["ClaireListing"]) -> List["ClaireListing"]:
				"""Filters listings down to the ones that are not spam
				and the ones that have not been sent."""
				filtered_listings: List["ClaireListing"] = []
				for listing in listings:
						# skip if sent
						if listing.id in self.sent_listings:
								continue

						filtered_listings.append(listing)
				return filtered_listings

		# def is_spam(self, spam_model: "ClaireSpam", listing: "ClaireListing") -> bool:
		#     """
		#     If the probability that a listing is spam
		#     exceeds the threshold, it is flagged as spam
		#     """
		#     prob_spam = spam_model.probability_of_spam(listing.details) * 100
		#     if prob_spam > self.spam_probability:
		#         return True

		#     return False

		def clean_listings(self, listings: List["ClaireListing"]) -> List["ClaireListing"]:
				"""Cleans up the listings to prepare for sending."""
				clean_listings = []
				for listing in listings:
						listing.clean()
						clean_listings.append(listing)
				return clean_listings

		def mark_sent(self, listings: List["ClaireListing"]):
				for listing in listings:
						self.sent_listings.add(listing.id)

		def send_listings(self, listings: List["ClaireListing"]):
				"""Sends the listings"""
				print(f"Here are some new listings for {self.keywords}:")

				for listing in listings:
						listing.display()
						print("\n" + "-" * 80 + "\n")


class ClaireListing:
		def __init__(
				self,
				source: object,
				id: str,
				name: str,
				url: str,
				posted: datetime,
				email: str,
				price: float,
				images: List[str],
				details: str,
				attributes: List[str],
		) -> None:
				self.source = source
				self.id = id
				self.email = email
				self.name = name
				self.url = url
				self.posted = posted
				self.price = price
				self.images = images
				self.details = details
				self.attributes = attributes

		def main_photo(self):
				"""Returns first image"""
				if self.images:
						for image in self.images:
								if "images" in image:
										if "http" in image:
												return image

		def clean(self):
				"""Cleans up the body of the listing by removing short sentences and links"""
				if self.details:
						try:
								# Removes links and short sentences from the body
								body = [
										sentence for sentence in self.details.split("\n") if "http" not in sentence and len(sentence) > 2
								]
								body = "\n".join(body)
						except Exception as e:
								print("Error", e)
						finally:
								self.body = body
				else:
						self.body = "Couldn't get details; post might have been deleted."

		def display(self):
				"""Displays the listing in a formatted manner for CLI"""
				posted = self.posted.strftime("%m/%d at %H:%M")
				main_photo = self.main_photo()

				print(f"Source: {self.source}")
				print(f"ID: {self.id}")
				print(f"Name: {self.name}")
				print(f"URL: {self.url}")
				print(f"Posted: {posted}")
				print(f"Price: ${self.price}")
				print(f"Main Photo: {main_photo}")
				print(f"Details: {self.details}")
				print(f"E-mail: {self.email}")

				print("Attributes:")
				for attribute in self.attributes:
						if len(attribute.split(": ")) == 1:
								continue

						kind = attribute.split(": ")[0]
						description = attribute.split(": ")[1]
						print(f"{kind.title()}: {description.title()}")


class Craigslist:
		@classmethod
		def get(cls, url) -> ClaireListing:
				listing = get_url(url)
				return ClaireListing(
						email=listing.get("email"),
						source=cls.__name__,
						id=listing.get("id"),
						name=listing.get("name", "Manual Pull"),
						url=listing.get("url"),
						posted=datetime.strptime(listing.get("created", "2001-01-01 00:00"), "%Y-%m-%d %H:%M"),
						price=listing.get("price").split("$")[1] if listing.get("price") else None,
						images=listing.get("images"),
						details=listing.get("body"),
						attributes=listing.get("attrs"),
				)

		@classmethod
		def search(cls, query: "ClaireQuery") -> List[ClaireListing]:
				listings: List["ClaireListing"] = []

				"""Uses the query to search Craigslist.
				Returns a list of ALL matching posts"""
				# Iterate through keywords and search CL
				for keyword in query.keywords.split(", "):
						# Searches CL with the parameters
						generator = CraigslistForSale(
								site=query.site,
								category=query.category,
								filters={
										"query": keyword,
										"max_price": query.budget,
										"has_image": query.has_image,
										"zip_code": query.zip_code,
										"search_distance": query.distance,
										"search_titles": False,
										"posted_today": True,
										"bundle_duplicates": True,
										"min_price": 0,
								},
						)
						# Adds listings to a list, include details returns an error if listing doesn't have body
						try:
								for listing in generator.get_results(sort_by="newest", include_details=True):
										if listing:
												listings.append(
														ClaireListing(
																email=listing.get("email"),
																source=cls.__name__,
																id=listing.get("id"),
																name=listing.get("name", "Unknown?"),
																url=listing.get("url"),
																posted=datetime.strptime(
																		listing.get("created", "2001-01-01 00:00"),
																		"%Y-%m-%d %H:%M",
																),
																price=listing.get("price").split("$")[1] if listing.get("price") else None,
																images=listing.get("images"),
																details=listing.get("body"),
																attributes=listing.get("attrs"),
														)
												)
						except Exception:
								for listing in generator.get_results(sort_by="newest", include_details=True):
										if listing:
												listings.append(
														ClaireListing(
																email=listing.get("email", "Unknown?"),
																source=cls.__name__,
																id=listing.get("id", "Unknown?"),
																name=listing.get("name", "Unknown?"),
																url=listing.get("url"),
																posted=datetime.strptime(
																		listing.get("created", "2001-01-01 00:00"),
																		"%Y-%m-%d %H:%M",
																),
																price=listing.get("price").split("$")[1] if listing.get("price") else None,
																images=listing.get("images", []),
																details=listing.get("body", "No details"),
																attributes=listing.get("attrs", {}),
														)
												)

				return listings


def listings_to_json(listings: List[ClaireListing], file_name: str):
		# Convert ClaireListing objects to dictionaries
		listings_dicts = []
		for listing in listings:
				listing_dict = {
						"source": listing.source,
						"email": listing.email,
						"id": listing.id,
						"name": listing.name,
						"url": listing.url,
						"posted": listing.posted.strftime("%Y-%m-%d %H:%M"),
						"price": listing.price,
						"images": listing.images,
						"details": json.dumps(listing.details),  # Jsonify details section
						"attributes": listing.attributes,
				}
				listings_dicts.append(listing_dict)

		# Write the list of dictionaries to a JSON file
		with open(file_name, "w") as json_file:
				json.dump(listings_dicts, json_file, indent=4)


class Sources(Enum):
		CRAIGSLIST = Craigslist

		def is_valid(self) -> bool:
				return isinstance(self.value, Searchable)

		@classmethod
		def get_name(cls, src: object) -> str:
				for source in cls:
						if source.value == src:
								return source.name


query = ClaireQuery(
		owner_id=1,
		zip_code=11249,
		state="NY",
		site="newyork",
		lat=40.7128,
		lon=-74.0060,
		keywords="apple",
		budget=30000,
		distance=100,
)
listings = Craigslist.search(query)
listings_to_json(listings, "listing3s.json")
print(f"Found {len(listings)} listings. and saved to listings.json")


filtered_listings = query.filter_listings(listings=listings)
clean_listings = query.clean_listings(filtered_listings)
query.send_listings(listings=clean_listings)
