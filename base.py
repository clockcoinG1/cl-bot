import logging
import re  # PY3
from queue import Queue
from threading import Thread
from urllib.parse import urljoin  # PY3

from aicaptcha import AiCaptcha
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from six import iteritems
from six.moves import range

import utils
from browser import CraigslistBrowser

solver = AiCaptcha("a433a493-bc2c-41a7-baab-fb898442d2ed")
user_agent = "Mozilla/5.0"
# ALL_SITES = utils		.get_all_sites()  # All the Craiglist sites
RESULTS_PER_REQUEST = 100  # Craigslist returns 100 results per request


class CraigslistBase(object):
		"""Base class for all Craiglist wrappers."""

		url_templates = {
				"base": "https://%(site)s.craigslist.org",
				"no_area": "https://%(site)s.craigslist.org/search/%(category)s",
				"area": "https://%(site)s.craigslist.org/search/%(area)s/%(category)s",
		}

		default_site = "newyork"
		default_category = None

		base_filters = {
				"query": {"url_key": "query", "value": None},
				"search_titles": {"url_key": "srchType", "value": "T"},
				"has_image": {"url_key": "hasPic", "value": 1},
				"posted_today": {"url_key": "postedToday", "value": 1},
				"bundle_duplicates": {"url_key": "bundleDuplicates", "value": 1},
				"search_distance": {"url_key": "search_distance", "value": None},
				"zip_code": {"url_key": "postal", "value": None},
		}
		extra_filters = {}
		__list_filters = {}  # Cache for list filters requested by URL

		# Set to True to subclass defines the customize_results() method
		custom_result_fields = False

		sort_by_options = {
				"newest": "date",
				"price_asc": "priceasc",
				"price_desc": "pricedsc",
		}

		def __init__(
				self,
				site=None,
				area=None,
				category=None,
				filters=None,
				log_level=logging.DEBUG,
		):
				# Logging
				self.set_logger(log_level, init=True)

				self.site = site or self.default_site
				self.area = area

				self.category = category or self.default_category

				url_template = self.url_templates["area" if area else "no_area"]
				self.url = url_template % {
						"site": self.site,
						"area": self.area,
						"category": self.category,
				}

				self.filters = self.get_filters(filters)

		def quit(self):
				CraigslistBrowser.quit()

		def get_filters(self, filters):
				"""Parses filters passed by the user into GET parameters."""

				list_filters = self.get_list_filters(self.url)

				# If a search has few results, results for "similar listings" will be
				# included. The solution is a bit counter-intuitive, but to force this
				# not to happen, we set searchNearby=True, but not pass any
				# nearbyArea=X, thus showing no similar listings.
				parsed_filters = {"searchNearby": 1}

				for key, value in iteritems((filters or {})):
						try:
								filter_ = self.base_filters.get(key) or self.extra_filters.get(key) or list_filters[key]
								if filter_["value"] is None:
										parsed_filters[filter_["url_key"]] = value
								elif isinstance(filter_["value"], dict):
										valid_options = filter_["value"]
										if not utils.isiterable(value) or isinstance(value, str):
												value = [value]  # Force to list
										options = []
										for opt in value:
												try:
														options.append(valid_options[opt])
												except KeyError:
														self.logger.warning("'%s' is not a valid option for %s" % (opt, key))
										parsed_filters[filter_["url_key"]] = options
								elif value:  # Don't add filter if ...=False
										parsed_filters[filter_["url_key"]] = filter_["value"]
						except KeyError:
								self.logger.warning("'%s' is not a valid filter", key)

				return parsed_filters

		def set_logger(self, log_level, init=False):
				if init:
						self.logger = logging.getLogger("python-craiglist")
						self.handler = logging.StreamHandler()
						self.logger.addHandler(self.handler)
				self.logger.setLevel(log_level)
				self.handler.setLevel(log_level)

		def get_results_approx_count(self, soup=None):
				"""
				Gets (approx) amount of results to be returned by `get_results`.

				Note that this number could be not exactly the same as the actual
				len of results returned (although from my tests usually within +/-10).
				Also note that this will make an extra request to Craigslist (if `soup`
				is not provided).
				"""

				if soup is None:
						page_source = utils.requests_get(self.url, params=self.filters, logger=self.logger)
						if not page_source:
								return 0

						soup = utils.bs(page_source)

				totalcount = soup.find("span", {"class": "totalcount"})
				return int(totalcount.text) if totalcount else None

		def get_results(self, limit=None, start=0, sort_by=None, geotagged=False, include_details=False):
				"""
				Gets results from Craigslist based on the specified filters.

				If geotagged=True, the results will include the (lat, lng) in the
				'geotag' attrib (this will make the process a little bit longer).
				"""

				if sort_by:
						try:
								self.filters["sort"] = self.sort_by_options[sort_by]
						except KeyError:
								msg = "'%s' is not a valid sort_by option, use: 'newest', 'price_asc' or 'price_desc'" % sort_by
								self.logger.error(msg)
								raise ValueError(msg)

				total_so_far = start
				results_yielded = 0
				total = 0

				while True:
						self.filters["s"] = start
						page_source = utils.requests_get(self.url, params=self.filters, logger=self.logger, wait=True)

						if not page_source:
								break

						soup = utils.bs(page_source)
						if not total:
								total = self.get_results_approx_count(soup=soup)

						rows = soup.find("ol")
						result_count = rows.find("div", {"class": "result-count"})
						limit = int(result_count.text.split(" ")[0])

						for row in rows.find_all("li", {"class": "cl-search-result"}, recursive=False):
								if limit is not None and results_yielded >= limit:
										break

								self.logger.debug(
										"Processing %s of %s results ...",
										total_so_far + 1,
										total or "(undefined)",
								)

								yield self.process_row(row, geotagged, include_details)

								results_yielded += 1
								total_so_far += 1

						if (total_so_far - start) < RESULTS_PER_REQUEST:
								break

						start = total_so_far

		def process_row(self, row, geotagged=False, include_details=False):
				if row.find("div", {"class": "gallery-card spacer"}) is not None:
						self.logger.debug("found a spacer, skipping")
						return

				id = row.attrs["data-pid"]
				repost_of = row.attrs.get("data-repost-of")

				link = row.find("a", {"class": "main"})
				name = row.attrs.get("title")
				url = urljoin(self.url, link.attrs["href"])
				price = row.find("span", {"class": "priceinfo"})
				has_pic = row.find("div", {"class": "dots"}) is not None

				meta = row.find("div", {"class": "meta"})
				if meta is not None:
						meta_text = meta.text
						metas = meta_text.split("·")
						location = metas[-1]

				result = {
						"id": id,
						"repost_of": repost_of,
						"name": name,
						"url": url,
						# NOTE: Keeping 'datetime' for backwards
						# compatibility, use 'last_updated' instead.
						"datetime": None,
						"last_updated": None,
						"price": price.text if price else None,
						"where": location if location else None,
						"has_image": has_pic,
						"geotag": None,
						# In very few cases, a posting will be included in the result
						# list but it has already been deleted (or it has been
						# deleted after the list was retrieved). In that case, this
						# field will be marked as True. If you want to be extra
						# careful, always check this field is False before using a
						# result.
						"deleted": False,
				}

				if geotagged or include_details:
						detail_soup = self.fetch_content(result["url"])
						if detail_soup:
								if geotagged:
										self.geotag_result(result, detail_soup)
								if include_details:
										self.include_details(result, detail_soup)

				if self.custom_result_fields:
						self.customize_result(result)

				return result

		def customize_result(self, result):
				"""Adds custom/delete/alter fields to result."""
				# Override in subclass to add category-specific fields.
				# FYI: `attrs` will only be presented if include_details was True.
				pass

		def geotag_result(self, result, soup):
				"""Adds (lat, lng) to result."""

				self.logger.debug("Geotagging result ...")

				map_ = soup.find("div", {"id": "map"})
				if map_:
						result["geotag"] = (
								float(map_.attrs["data-latitude"]),
								float(map_.attrs["data-longitude"]),
						)

				return result

		def include_details(self, result, soup):
				"""Adds description, images to result"""
				self.logger.debug("Adding details to result...")
				body = soup.find("section", id="postingbody")
				if not body:
						result["deleted"] = True
						return

				body_text = (getattr(e, "text", e) for e in body if not getattr(e, "attrs", None))
				result["body"] = "".join(body_text).strip()
				driver = CraigslistBrowser.get_driver()
				driver.get(result["url"])

				try:
						wait = WebDriverWait(driver, 10)
						button = wait.until(EC.element_to_be_clickable((By.XPATH, '//button[@class="reply-button js-only"]')))
						button.click()

						show_email_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//button[@class="show-email"]')))
						show_email_button.click()

						email_element = wait.until(EC.visibility_of_element_located((By.XPATH, '//a[contains(@href, "mailto:")]')))
						email_address = email_element.get_attribute("href").replace("mailto:", "").split("?")[0]

						result["email"] = email_address
						print(email_address)
				except Exception as e:
						captcha_data = driver.execute_script("return document.querySelector('div.h-captcha iframe');")
						if captcha_data:
								src = captcha_data.get_attribute('src')

								# Extract sitekey
								sitekey = src.split('sitekey=')[1].split('&')[0]

								# Extract page_url
								page_url = src.split('origin=')[1].split('&')[0]

								hcaptcha_result = solver.Hcaptcha(site_key=sitekey, page_url=page_url, user_agent=user_agent)
								print(hcaptcha_result.token)
								print(hcaptcha_result.key)
						self.logger.error(f"Error while getting email address: {e}")
				try:

						show_phone_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//button[@class="show-phone"]')))
						show_phone_button.click()

						phone_element = wait.until(EC.visibility_of_element_located((By.XPATH, '//span[@class="phone"]')))
						phone_number = phone_element.text

						result["phone"] = phone_number
						print(phone_number)
				except Exception as e:
						self.logger.error(f"Error while getting phone number: {e}")
				# Add created time (in case it's different from last updated).
				postinginfos = soup.find("div", {"class": "postinginfos"})
				for p in postinginfos.find_all("p"):
						if "posted" in p.text:
								time = p.find("time")
								if time:
										# This date is in ISO format. I'm removing the T literal
										# and the timezone to make it the same format as
										# 'last_updated'.
										created = time.attrs["datetime"].replace("T", " ")
										result["created"] = created.rsplit(":", 1)[0]

				# Add images' urls.
				image_tags = soup.find_all("img")
				# If there's more than one picture, the first one will be repeated.
				image_tags = image_tags[1:] if len(image_tags) > 1 else image_tags
				images = []
				for img in image_tags:
						try:
								img_link = img["src"].replace("50x50c", "600x450")
								images.append(img_link)
						except KeyError:
								continue  # Some posts contain empty <img> tags.
				result["images"] = images

				# Add list of attributes as unparsed strings. These values are then
				# processed by `parse_attrs`, and are available to be post-processed
				# by subclasses.
				attrgroups = soup.find_all("p", {"class": "attrgroup"})
				attrs = []
				for attrgroup in attrgroups:
						for attr in attrgroup.find_all("span"):
								attr_text = attr.text.strip()
								if attr_text:
										attrs.append(attr_text)
				result["attrs"] = attrs
				if attrs:
						self.parse_attrs(result)

				# If an address is included, add it to `address`.
				mapaddress = soup.find("div", {"class": "mapaddress"})
				if mapaddress:
						result["address"] = mapaddress.text

		def parse_attrs(self, result):
				"""Parses raw attributes into structured fields in the result dict."""

				# Parse binary fields first by checking their presence.
				attrs = set(attr.lower() for attr in result["attrs"])
				for key, options in iteritems(self.extra_filters):
						if options["value"] != 1:
								continue  # Filter is not binary
						if options.get("attr", "") in attrs:
								result[key] = True
				# Values from list filters are sometimes shown as {filter}: {value}
				# e.g. "transmission: automatic", although usually they are shown only
				# with the {value}, e.g. "laundry in bldg". By stripping the content
				# before the colon (if any) we reduce it to a single case.
				attrs_after_colon = set(attr.split(": ", 1)[-1] for attr in result["attrs"])
				for key, options in iteritems(self.get_list_filters(self.url)):
						for option in options["value"].keys():
								if option in attrs_after_colon:
										result[key] = option
										break

		def fetch_content(self, url):
				page_source = utils.requests_get(url, logger=self.logger)

				if not page_source:
						return

				# if response.ok:
				#     return utils.bs(response.page_source)
				return utils.bs(page_source)
				# self.logger.warning("GET %s returned not OK response code: %s "
				#                     "(skipping)", url, response.status_code)
				# return None

		def geotag_results(self, results, workers=8):
				"""
				Adds (lat, lng) to each result. This process is done using N threads,
				where N is the amount of workers defined (default: 8).
				"""

				results = list(results)
				queue = Queue()

				for result in results:
						queue.put(result)

				def geotagger():
						while not queue.empty():
								self.logger.debug("%s results left to geotag ...", queue.qsize())
								self.geotag_result(queue.get())
								queue.task_done()

				threads = []
				for _ in range(workers):
						thread = Thread(target=geotagger)
						thread.start()
						threads.append(thread)

				for thread in threads:
						thread.join()
				return results

		@classmethod
		def get_list_filters(cls, url):
				if cls.__list_filters.get(url) is None:
						cls.__list_filters[url] = utils.get_list_filters(url)
				return cls.__list_filters[url]

		@classmethod
		def show_categories(cls):
				url = cls.url_templates["no_area"] % {
						"site": cls.default_site,
						"category": cls.default_category,
				}
				page_source = utils.requests_get(url)
				soup = utils.bs(page_source)

				cat_html = soup.find_all("input", {"class": "catcheck multi_checkbox"})
				cat_ids = [html.get("data-abb") for html in cat_html]
				cat_html = soup.find_all("a", {"class": "category"})
				cat_names = [html.contents[0] for html in cat_html]

				print("%s categories:" % cls.__name__)
				for cat_name, cat_id in sorted(zip(cat_names, cat_ids)):
						print("* %s = %s" % (cat_id, cat_name))
				CraigslistBrowser.quit()

		@classmethod
		def show_filters(cls, category=None):
				print("Base filters:")
				for key, options in iteritems(cls.base_filters):
						value_as_str = "..." if options["value"] is None else "True/False"
						print("* %s = %s" % (key, value_as_str))

				if category is None:
						print("\n%s filters:" % cls.__name__)
				else:
						print("\n%s filters for category '%s':" % (cls.__name__, category))
				for key, options in iteritems(cls.extra_filters):
						value_as_str = "..." if options["value"] is None else "True/False"
						print("* %s = %s" % (key, value_as_str))
				url = cls.url_templates["no_area"] % {
						"site": cls.default_site,
						"category": category or cls.default_category,
				}
				list_filters = cls.get_list_filters(url)
				for key, options in iteritems(list_filters):
						value_as_str = ", ".join(repr(opt) for opt in options["value"].keys())
						print("* %s = %s" % (key, value_as_str))
