import sys
import time
from abc import (
    ABCMeta,
    abstractmethod,
)
import requests
import logging
import re
import datetime
import yaml
import json
from enum import Enum
from typing import List, Dict
import os
from pprint import (
    pformat,
    pprint,
)
import html
from colorama import Fore, Style

from data.releaseskeywords import *
from data.articleskeywords import *
from data.digestrecordcategory import *
from data.digestrecordstate import *
from data.digestrecordsubcategory import *

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


SCRIPT_DIRECTORY = os.path.dirname(os.path.realpath(__file__))
DIGEST_RECORD_DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S %z'

days_count = None


class Formatter(logging.Formatter):

    def __init__(self, fmt=None):
        if fmt is None:
            fmt = self._colorized_fmt()
        logging.Formatter.__init__(self, fmt)

    def _colorized_fmt(self, color=Fore.RESET):
        return f'{color}[%(asctime)s] %(levelname)s: %(message)s{Style.RESET_ALL}'

    def format(self, record):
        # Save the original format configured by the user
        # when the logger formatter was instantiated
        format_orig = self._style._fmt

        # Replace the original format with one customized by logging level
        if record.levelno == logging.DEBUG:
            color = Fore.CYAN
        elif record.levelno == logging.INFO:
            color = Fore.GREEN
        elif record.levelno == logging.WARNING:
            color = Fore.YELLOW
        elif record.levelno == logging.ERROR:
            color = Fore.RED
        elif record.levelno == logging.CRITICAL:
            color = Fore.MAGENTA
        else:
            color = Fore.WHITE
        self._style._fmt = self._colorized_fmt(color)

        # Call the original formatter class to do the grunt work
        result = logging.Formatter.format(self, record)

        # Restore the original format configured by the user
        self._style._fmt = format_orig

        return result


class Logger(logging.Logger):

    def __init__(self):
        super().__init__('fntools')
        h = logging.StreamHandler(sys.stderr)
        f = Formatter()
        h.setFormatter(f)
        h.flush = sys.stderr.flush
        self.addHandler(h)
        self.setLevel(logging.INFO)


logger = Logger()


class NetworkingMixin:
    SLEEP_BETWEEN_ATTEMPTS_SECONDS = 5
    NETWORK_TIMEOUT_SECONDS = 10
    NETWORK_RETRIES_COUNT = 50

    class RequestType(Enum):
        GET = 'GET'
        PATCH = 'PATCH'
        POST = 'POST'

    def get_with_retries(self, url, headers=None):
        return self.request_with_retries(url, headers=headers, method=self.RequestType.GET, data=None)

    def patch_with_retries(self, url, headers=None, data=None):
        return self.request_with_retries(url, headers=headers, method=self.RequestType.PATCH, data=data)

    def post_with_retries(self, url, headers=None, data=None):
        return self.request_with_retries(url, headers=headers, method=self.RequestType.POST, data=data)

    def request_with_retries(self, url, headers=None, method=RequestType.GET, data=None):
        if headers is None:
            headers = {}
        for attempt_i in range(self.NETWORK_RETRIES_COUNT):
            begin_datetime = datetime.datetime.now()
            try:
                if method == self.RequestType.GET:
                    logger.debug(f'GETting URL "{url}"')
                    response = requests.get(url,
                                            headers=headers,
                                            timeout=self.NETWORK_TIMEOUT_SECONDS)
                elif method == self.RequestType.PATCH:
                    logger.debug(f'PATCHing URL "{url}"')
                    response = requests.patch(url,
                                              data=data,
                                              headers=headers,
                                              timeout=self.NETWORK_TIMEOUT_SECONDS)
                elif method == self.RequestType.POST:
                    logger.debug(f'POSTing URL "{url}"')
                    response = requests.post(url,
                                             data=data,
                                             headers=headers,
                                             timeout=self.NETWORK_TIMEOUT_SECONDS)
                else:
                    raise NotImplementedError
                end_datetime = datetime.datetime.now()
                logger.debug(f'Response time: {end_datetime - begin_datetime}')
                return response
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
                base_timeout_msg = f'Request to url {url} reached timeout of {self.NETWORK_TIMEOUT_SECONDS} seconds'
                if attempt_i != self.NETWORK_RETRIES_COUNT - 1:
                    logger.warning(f'{base_timeout_msg}, sleeping {self.SLEEP_BETWEEN_ATTEMPTS_SECONDS} seconds and trying again, {self.NETWORK_RETRIES_COUNT - attempt_i - 1} retries left')
                    time.sleep(self.SLEEP_BETWEEN_ATTEMPTS_SECONDS)
                else:
                    raise Exception(f'{base_timeout_msg}, retries count {self.NETWORK_RETRIES_COUNT} exceeded')


class BasicPostsStatisticsGetter(NetworkingMixin,
                                 metaclass=ABCMeta):

    def __init__(self):
        self._posts_urls = {}
        self.source_name = None

    def posts_statistics(self):
        posts_statistics = {}
        for number, url in self.posts_urls.items():
            views_count = self.post_statistics(number, url)
            posts_statistics[number] = views_count
            logger.info(f'Views count for {self.source_name} post #{number} ({url}): {views_count}')
            time.sleep(1)
        return posts_statistics

    @abstractmethod
    def post_statistics(self, number, url):
        pass

    @property
    def posts_urls(self):
        return self._posts_urls


class VkPostsStatisticsGetter(BasicPostsStatisticsGetter):

    def __init__(self):
        super().__init__()
        self.source_name = 'VK'
        self._posts_urls = {}
        self._posts_count = 1

    @property
    def posts_urls(self):
        if self._posts_urls == {}:
            for i in range(self._posts_count):
                self._posts_urls[i] = f'https://vk.com/@permlug-foss-news-{i}'
        return self._posts_urls

    def post_statistics(self, number, url):
        response = self.get_with_retries(url)
        content = response.text
        re_result = re.search(r'<div class="articleView__views_info">(\d+) просмотр', content)
        if re_result is None:
            logger.error(f'Failed to find statistics in FOSS News #{number} ({url}) on VK')
            return None
        return int(re_result.group(1))


class DigestRecord:

    def __init__(self,
                 dt: datetime.datetime,
                 source: str,
                 title: str,
                 url: str,
                 state: DigestRecordState = DigestRecordState.UNKNOWN,
                 digest_issue: int = None,
                 category: DigestRecordCategory = DigestRecordCategory.UNKNOWN,
                 subcategory: Enum = None,
                 drid: int = None,
                 is_main: bool = None,
                 keywords: List[str] = None,
                 language: str = None,
                 estimations: List = None):
        self.dt = dt
        self.source = source
        self.title = title
        self.url = url
        self.state = state
        self.digest_issue = digest_issue
        self.category = category
        self.subcategory = subcategory
        self.drid = drid
        self.is_main = is_main
        self.keywords = keywords
        self.language = language
        self.estimations = estimations

    def __str__(self):
        return pformat(self.to_dict())

    def to_dict(self):
        return {
            'drid': self.drid,
            'datetime': self.dt.strftime(DIGEST_RECORD_DATETIME_FORMAT) if self.dt is not None else None,
            'source': self.source,
            'title': self.title,
            'url': self.url,
            'is_main': self.is_main,
            'state': self.state.value if self.state is not None else None,
            'digest_issue': self.digest_issue,
            'category': self.category.value if self.category is not None else None,
            'subcategory': self.subcategory.value if self.subcategory is not None else None,
            'keywords': self.keywords,
            'language': self.language,
            'estimations': [{'user': e['user'],
                             'state': e['state'].value}
                            for e in self.estimations],
        }


class ServerConnectionMixin:
    # Requires NetworkingMixin

    def _load_config(self, config_path):
        logger.info(f'Loading gathering server connect data from config "{config_path}"')
        with open(config_path, 'r') as fin:
            config_data = yaml.safe_load(fin)
            self._host = config_data['host']
            self._protocol = config_data['protocol']
            self._port = config_data['port']
            self._user = config_data['user']
            self._password = config_data['password']
            logger.info('Loaded')

    def _login(self):
        logger.info('Logging in')
        url = f'{self._protocol}://{self._host}:{self._port}/api/v1/token/'
        data = {'username': self._user, 'password': self._password}
        response = self.post_with_retries(url=url,
                                          data=data)
        if response.status_code != 200:
            raise Exception(f'Invalid response code from FNGS login - {response.status_code}: {response.content.decode("utf-8")}')
        result_data = json.loads(response.content)
        self._token = result_data['access']
        logger.info('Logged in')

    @property
    def api_url(self):
        return f'{self._protocol}://{self._host}:{self._port}/api/v1'

    @property
    def _auth_headers(self):
        return {
            'Authorization': f'Bearer {self._token}',
            'Content-Type': 'application/json',
        }


class HabrPostsStatisticsGetter(BasicPostsStatisticsGetter,
                                NetworkingMixin,
                                ServerConnectionMixin):

    def __init__(self, config_path):
        super().__init__()
        self.source_name = 'Habr'
        self.driver = None
        self._load_config(config_path)
        self._login()
        self._posts_urls = {di['number']: di['habr_url'] for di in self._digest_issues}

    @property
    def _digest_issues(self):
        response = self.get_with_retries(f'{self.api_url}/digest-issues/', headers=self._auth_headers)
        content = response.text
        if response.status_code != 200:
            raise Exception(f'Failed to get digest issues info, status code {response.status_code}, response: {content}')
        content_data = json.loads(content)
        return content_data

    def posts_statistics(self):
        self.driver = webdriver.Firefox()
        statistics = super().posts_statistics()
        self.driver.close()
        return statistics

    def post_statistics(self, number, url):
        if not url:
            logger.error(f'Empty URL for digest issue #{number}')
            return None
        self.driver.get(url)
        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        xpath = '//div[contains(@class, "tm-page-article__body")]//span[contains(@class, "tm-icon-counter__value")]'
        element = WebDriverWait(self.driver, 20).until(EC.element_to_be_clickable((By.XPATH, xpath)))

        full_statistics_str = element.text
        logger.debug(f'Full statistics string for FOSS News #{number}: "{full_statistics_str}"')
        re_result = re.fullmatch(r'((\d+)([\.,](\d+))?)[Kk]?', full_statistics_str)
        if re_result is None:
            logger.error(f'Invalid statistics format in FOSS News #{number} ({url}) on Habr: {full_statistics_str}')
            return None

        statistics_without_k = re_result.group(1)
        statistics_before_comma = re_result.group(2)
        statistics_after_comma = re_result.group(4)

        if 'K' in full_statistics_str or 'k' in full_statistics_str:
            views_count = int(statistics_before_comma) * 1000
            if statistics_after_comma is not None:
                views_count += int(statistics_after_comma) * 100
        else:
            views_count = int(statistics_without_k)

        return views_count


# TODO: Refactor
class DigestRecordsCollection(NetworkingMixin,
                              ServerConnectionMixin):

    def __init__(self,
                 config_path: str,
                 records: List[DigestRecord] = None):
        self._config_path = config_path
        self.records = records if records is not None else []
        self.duplicates = []
        self._filtered_records = []
        self._token = None
        self._current_digest_issue = None

    def __str__(self):
        return pformat([record.to_dict() for record in self.records])

    def save_to_yaml(self, yaml_path: str):
        records_plain = []
        for record_object in self.records:
            record_plain = {
                'datetime': record_object.dt.strftime(DIGEST_RECORD_DATETIME_FORMAT)
                            if record_object.dt is not None
                            else None,
                'title': record_object.title,
                'url': record_object.url,
                'state': record_object.state.value if record_object.state is not None else None,
                'is_main': record_object.is_main,
                'digest_issue': record_object.digest_issue,
                'category': record_object.category.value if record_object.category is not None else None,
                'subcategory': record_object.subcategory.value if record_object.subcategory is not None else None,
            }
            records_plain.append(record_plain)
        with open(yaml_path, 'w') as fout:
            logger.info(f'Saving results to "{yaml_path}"')
            yaml.safe_dump(records_plain, fout)

    def load_specific_digest_records_from_server(self,
                                                 digest_issue: int):
        self._load_config(self._config_path)
        self._login()
        self._load_duplicates_for_specific_digest(digest_issue)
        self._basic_load_digest_records_from_server(f'{self._protocol}://{self._host}:{self._port}/api/v1/specific-digest-records/?digest_issue={digest_issue}')

    def _load_one_new_digest_record_from_server(self):
        self._basic_load_digest_records_from_server(f'{self._protocol}://{self._host}:{self._port}/api/v1/one-new-foss-news-digest-record/')

    def _load_tbot_categorization_data(self):
        self.records = []
        logger.info('Loading TBot categorization data')
        url = f'{self.api_url}/digest-records-categorized-by-tbot/'
        response = self.get_with_retries(url, headers=self._auth_headers)
        if response.status_code != 200:
            raise Exception(f'Failed to retrieve digest records duplicates, status code {response.status_code}, response: {response.content}')
        response_str = response.content.decode()
        response_data = json.loads(response_str)
        for digest_record_id, digest_record_data in response_data.items():
            if digest_record_data['dt'] is not None:
                dt_str = datetime.datetime.strptime(digest_record_data['dt'],
                                                    '%Y-%m-%dT%H:%M:%SZ')
            else:
                dt_str = None
            record_object = DigestRecord(dt_str,
                                         digest_record_data['source'],
                                         digest_record_data['title'],
                                         digest_record_data['url'],
                                         digest_issue=digest_record_data['digest_issue'],
                                         drid=digest_record_id,
                                         is_main=digest_record_data['is_main'],
                                         keywords=digest_record_data['title_keywords'],
                                         estimations=[{'user': e['user'],
                                                       'state': DigestRecordState(e['state'].lower())}
                                                      for e in digest_record_data['estimations']])
            self.records.append(record_object)


    def _load_duplicates_for_specific_digest(self,
                                             digest_issue: int):
        logger.info(f'Getting digest records duplicates for digest number #{digest_issue}')
        url = f'{self.api_url}/digest-records-duplicates-detailed/?digest_issue={digest_issue}'
        response = self.get_with_retries(url, headers=self._auth_headers)
        if response.status_code != 200:
            raise Exception(f'Failed to retrieve digest records duplicates, status code {response.status_code}, response: {response.content}')
        response_str = response.content.decode()
        response = json.loads(response_str)
        if not response:
            logger.info('No digest records duplicates found')
            return None
        response_converted = []
        for duplicate in response:
            duplicate_converted = {}
            for key in ('id', 'digest_issue'):
                duplicate_converted[key] = duplicate[key]
            if not duplicate['digest_records']:
                logger.warning(f'Empty digest records list in duplicate #{duplicate["id"]}')
                continue
            duplicate_converted['digest_records'] = []
            for record in duplicate['digest_records']:
                if record['dt'] is not None:
                    dt_str = datetime.datetime.strptime(record['dt'],
                                                        '%Y-%m-%dT%H:%M:%SZ')
                else:
                    dt_str = None
                record_obj = DigestRecord(dt_str,
                                          None,
                                          record['title'],
                                          record['url'],
                                          digest_issue=record['digest_issue'],
                                          drid=record['id'],
                                          is_main=record['is_main'],
                                          keywords=record['title_keywords'],
                                          language=record['language'])
                record_obj.state = DigestRecordState(record['state'].lower()) if 'state' in record and record['state'] is not None else None
                record_obj.category = DigestRecordCategory(record['category'].lower()) if 'category' in record and record['category'] is not None else None
                if 'subcategory' in record and record['subcategory'] == 'DATABASES':
                    record['subcategory'] = 'db'
                record_obj.subcategory = DigestRecordSubcategory(record['subcategory'].lower()) if 'subcategory' in record and record['subcategory'] is not None else None
                duplicate_converted['digest_records'].append(record_obj)
            response_converted.append(duplicate_converted)
        self.duplicates += response_converted


    def _basic_load_digest_records_from_server(self, url: str):
        records_objects: List[DigestRecord] = []
        logger.info('Getting digest records')
        response = self.get_with_retries(url, headers=self._auth_headers)
        if response.status_code != 200:
            raise Exception(
                f'Invalid response code from FNGS fetch - {response.status_code}: {response.content.decode("utf-8")}')
        logger.info('Got data')
        result_data = json.loads(response.content)
        for record_plain in result_data:
            if record_plain['dt'] is not None:
                dt_str = datetime.datetime.strptime(record_plain['dt'],
                                                    '%Y-%m-%dT%H:%M:%SZ')
            else:
                dt_str = None
            record_object = DigestRecord(dt_str,
                                         None,
                                         record_plain['title'],
                                         record_plain['url'],
                                         digest_issue=record_plain['digest_issue'],
                                         drid=record_plain['id'],
                                         is_main=record_plain['is_main'],
                                         keywords=[k['name'] for k in record_plain['title_keywords']] if record_plain['title_keywords'] else [],
                                         language=record_plain['language'],
                                         estimations=[{'user': e['telegram_bot_user']['username'],
                                                       'state': DigestRecordState(e['estimated_state'].lower())}
                                                      for e in record_plain['tbot_estimations']])
            record_object.state = DigestRecordState(record_plain['state'].lower()) if 'state' in record_plain and record_plain['state'] is not None else None
            record_object.category = DigestRecordCategory(record_plain['category'].lower()) if 'category' in record_plain and record_plain['category'] is not None else None
            if 'subcategory' in record_plain and record_plain['subcategory'] == 'DATABASES':
                record_plain['subcategory'] = 'db'
            record_object.subcategory = DigestRecordSubcategory(record_plain['subcategory'].lower()) if 'subcategory' in record_plain and record_plain['subcategory'] is not None else None
            records_objects.append(record_object)
        self.records = records_objects

    def _clear_title(self, title: str):
        fixed_title = html.unescape(title)
        fixed_title = re.sub(r'^\[.+?\]\s+', '', fixed_title)
        return fixed_title

    def _build_url_html(self, url: str, lang: str):
        if lang not in ('RUSSIAN', 'ENGLISH'):
            raise NotImplementedError(f'Unsupported language {lang} for url {url}')
        patterns_to_clear = (
            '\?rss=1$',
            r'#ftag=\w+$',
        )
        for pattern_to_clear in patterns_to_clear:
            url = re.sub(pattern_to_clear, '', url)
        return f'<a href="{url}">{url}</a>{" (en)" if lang == "ENGLISH" else ""}'

    def records_to_html(self, html_path):
        logger.info('Converting records to HTML')
        output_records = {
            'main': [],
            DigestRecordCategory.NEWS.value: {subcategory_value: [] for subcategory_value in DIGEST_RECORD_SUBCATEGORY_VALUES},
            DigestRecordCategory.VIDEOS.value: {subcategory_value: [] for subcategory_value in DIGEST_RECORD_SUBCATEGORY_VALUES},
            DigestRecordCategory.ARTICLES.value: {subcategory_value: [] for subcategory_value in DIGEST_RECORD_SUBCATEGORY_VALUES},
            DigestRecordCategory.RELEASES.value: {subcategory_value: [] for subcategory_value in DIGEST_RECORD_SUBCATEGORY_VALUES},
            DigestRecordCategory.OTHER.value: [],
        }
        digest_records_ids_from_duplicates = []
        for duplicate in self.duplicates:
            duplicate_records = duplicate['digest_records']
            first_in_duplicate = duplicate_records[0]
            if first_in_duplicate.state != DigestRecordState.IN_DIGEST:
                continue
            for duplicate_record in duplicate_records:
                digest_records_ids_from_duplicates.append(duplicate_record.drid)
            if first_in_duplicate.is_main:
                output_records['main'].append(duplicate_records)
            elif first_in_duplicate.category == DigestRecordCategory.OTHER:
                output_records[first_in_duplicate.category.value].append(duplicate_records)
            elif not first_in_duplicate.is_main and first_in_duplicate.category in (DigestRecordCategory.NEWS,
                                                                                    DigestRecordCategory.ARTICLES,
                                                                                    DigestRecordCategory.VIDEOS,
                                                                                    DigestRecordCategory.RELEASES):
                if first_in_duplicate.subcategory is not None:
                    output_records[first_in_duplicate.category.value][first_in_duplicate.subcategory.value].append(duplicate_records)
            else:
                pprint(duplicate)
                raise NotImplementedError
        for digest_record in self.records:
            if digest_record.state != DigestRecordState.IN_DIGEST:
                continue
            if digest_record.drid in digest_records_ids_from_duplicates:
                continue
            if digest_record.is_main:
                output_records['main'].append(digest_record)
            elif digest_record.category == DigestRecordCategory.OTHER:
                output_records[digest_record.category.value].append(digest_record)
            elif not digest_record.is_main and digest_record.category in (DigestRecordCategory.NEWS,
                                                                          DigestRecordCategory.ARTICLES,
                                                                          DigestRecordCategory.VIDEOS,
                                                                          DigestRecordCategory.RELEASES):
                if digest_record.subcategory is not None:
                    output_records[digest_record.category.value][digest_record.subcategory.value].append(digest_record)
            else:
                print(digest_record.title, digest_record.category, digest_record.is_main)
                raise NotImplementedError
        output = '<h2>Главное</h2>\n\n'
        for main_record in output_records['main']:
            if not isinstance(main_record, list):
                output += f'<h3>{self._clear_title(main_record.title)}</h3>\n\n'
                output += f'<i><b>Категория</b>: {DIGEST_RECORD_CATEGORY_RU_MAPPING[main_record.category.value]}/{DIGEST_RECORD_SUBCATEGORY_RU_MAPPING[main_record.subcategory.value]}</i><br>\n\n'
                output += f'Подробности {self._build_url_html(main_record.url, main_record.language)}\n\n'
            else:
                output += f'<h3>{[self._clear_title(r.title) for r in main_record]}</h3>\n\n'
                output += f'<i><b>Категория</b>: {DIGEST_RECORD_CATEGORY_RU_MAPPING[main_record[0].category.value]}/{DIGEST_RECORD_SUBCATEGORY_RU_MAPPING[main_record[0].subcategory.value]}</i><br>\n\n'
                output += 'Подробности:<br>\n\n'
                output += '<ol>\n'
                for r in main_record:
                    output += f'<li>{r.title} {self._build_url_html(r.url, r.language)}</li>\n\n'
                output += '</ol>\n'

        output += '<h2>Короткой строкой</h2>\n\n'

        keys = (
            DigestRecordCategory.NEWS.value,
            DigestRecordCategory.VIDEOS.value,
            DigestRecordCategory.ARTICLES.value,
            DigestRecordCategory.RELEASES.value,
        )
        for key in keys:
            output += f'<h3>{DIGEST_RECORD_CATEGORY_RU_MAPPING[key]}</h3>\n\n'
            for key_record_subcategory, key_records in output_records[key].items():
                if not key_records:
                    continue
                output += f'<h4>{DIGEST_RECORD_SUBCATEGORY_RU_MAPPING[key_record_subcategory]}</h4>\n\n'
                if len(key_records) == 1:
                    key_record = key_records[0]
                    if not isinstance(key_record, list):
                        output += f'<p>{self._clear_title(key_record.title)} {self._build_url_html(key_record.url, key_record.language)}</p>\n'
                    else:
                        output += f'<p>{[self._clear_title(r.title) for r in key_record]} {", ".join([self._build_url_html(r.url, r.language) for r in key_record])}</p>\n'
                else:
                    output += '<ol>\n'
                    for key_record in key_records:
                        if not isinstance(key_record, list):
                            output += f'<li>{self._clear_title(key_record.title)} {self._build_url_html(key_record.url, key_record.language)}</li>\n'
                        else:
                            output += f'<li>{[self._clear_title(r.title) for r in key_record]} {", ".join([self._build_url_html(r.url, r.language) for r in key_record])}</li>\n'
                    output += '</ol>\n'

        if len(output_records[DigestRecordCategory.OTHER.value]):
            output += '<h2>Что ещё посмотреть</h2>\n\n'
            if len(output_records[DigestRecordCategory.OTHER.value]) == 1:
                other_record = output_records[DigestRecordCategory.OTHER.value][0]
                output += f'{self._clear_title(other_record.title)} {self._build_url_html(other_record.url, other_record.language)}<br>\n'
            else:
                output += '<ol>\n'
                for other_record in output_records[DigestRecordCategory.OTHER.value]:
                    output += f'<li>{self._clear_title(other_record.title)} {self._build_url_html(other_record.url, other_record.language)}</li>\n'
                output += '</ol>\n'

        logger.info('Converted')
        with open(html_path, 'w') as fout:
            logger.info(f'Saving output to "{html_path}"')
            fout.write(output)

    def _guess_category(self, title: str, url: str) -> DigestRecordCategory:
        if 'https://www.youtube.com' in url:
            return DigestRecordCategory.VIDEOS
        if 'SD Times Open-Source Project of the Week' in title:
            return DigestRecordCategory.OTHER
        if 'Еженедельник OSM' in title:
            return DigestRecordCategory.NEWS
        for release_keyword in RELEASES_KEYWORDS:
            if release_keyword.lower() in title.lower():
                return DigestRecordCategory.RELEASES
        for article_keyword in ARTICLES_KEYWORDS:
            if article_keyword.lower() in title.lower():
                return DigestRecordCategory.ARTICLES

        for keyword_data in self._keywords():
            if not keyword_data['is_generic']:
                keyword_name_fixed = keyword_data['name'].replace('+', r'\+')
                if re.search(keyword_name_fixed + r',?\s+v?\.?\d', title, re.IGNORECASE):
                    return DigestRecordCategory.RELEASES
        return None

    def _keywords(self):
        url = f'{self.api_url}/keywords'
        response = self.get_with_retries(url, self._auth_headers)
        if response.status_code != 200:
            logger.error(
                f'Failed to retrieve guessed subcategories, status code {response.status_code}, response: {response.content}')
            # TODO: Raise exception and handle above
            return None
        response_str = response.content.decode()
        response = json.loads(response_str)
        return response

    def _show_similar_from_previous_digest(self, keywords: List[str]):
        if not keywords:
            logger.debug('Could not search for similar records from previous digest cause keywords list is empty')
            return
        url = f'{self.api_url}/similar-records-in-previous-digest/?keywords={",".join(keywords)}&current-digest-number={self._current_digest_issue}'
        response = self.get_with_retries(url, self._auth_headers)
        if response.status_code != 200:
            logger.error(f'Failed to retrieve guessed subcategories, status code {response.status_code}, response: {response.content}')
            # TODO: Raise exception and handle above
            return None
        response_str = response.content.decode()
        response = json.loads(response_str)
        if response and 'similar_records_in_previous_digest' in response:
            similar_records_in_previous_digest = response['similar_records_in_previous_digest']
            if similar_records_in_previous_digest:
                print(f'Similar records in previous digest:')
                for record in similar_records_in_previous_digest:
                    is_main_ru = "главная" if record["is_main"] else "не главная"
                    if record["category"]:
                        category_ru = DIGEST_RECORD_CATEGORY_RU_MAPPING[DigestRecordCategory.from_name(record["category"]).value].lower()
                    else:
                        category_ru = None
                    if record["subcategory"]:
                        subcategory_ru = DIGEST_RECORD_SUBCATEGORY_RU_MAPPING[DigestRecordSubcategory.from_name(record["subcategory"]).value].lower()
                    else:
                        subcategory_ru = None
                    print(f'- {record["title"]} ({is_main_ru}, {category_ru}, {subcategory_ru}) - {record["url"]}')

    def _guess_subcategory(self, title: str) -> (List[DigestRecordSubcategory], Dict):
        if 'Еженедельник OSM' in title:
            return [DigestRecordSubcategory.ORG], {}

        url = f'{self.api_url}/guess-category/?title={title}'
        response = self.get_with_retries(url, self._auth_headers)
        if response.status_code != 200:
            logger.error(f'Failed to retrieve guessed subcategories, status code {response.status_code}, response: {response.content}')
            # TODO: Raise exception and handle above
            return None
        response_str = response.content.decode()
        response = json.loads(response_str)
        # TODO: Check title
        matches = response['matches']

        guessed_subcategories: List[DigestRecordSubcategory] = []
        matched_subcategories_keywords = {}

        for guessed_subcategory_name, matched_keywords in matches.items():
            if not guessed_subcategory_name or guessed_subcategory_name == 'null':
                continue
            subcategory = DigestRecordSubcategory(guessed_subcategory_name.lower()
                                                  if guessed_subcategory_name != 'DATABASES'
                                                  else 'db')
            guessed_subcategories.append(subcategory)
            matched_subcategories_keywords[guessed_subcategory_name.lower()] = matched_keywords

        return guessed_subcategories, matched_subcategories_keywords

    def categorize_interactively(self):
        self._load_config(self._config_path)
        self._login()
        while True:
            if self._current_digest_issue is None:
                self._current_digest_issue = self._ask_digest_issue()
            # self._print_non_categorized_digest_records_count()
            self._load_tbot_categorization_data()
            if self.records:
                # TODO: Think how to process left record in non-conflicting with Tbot usage way
                self._categorize_records_from_tbot()
                # self._print_non_categorized_digest_records_count()
            if not self.records:
                self._load_one_new_digest_record_from_server()
            self._categorize_new_records()
            self._print_non_categorized_digest_records_count()
            if self._non_categorized_digest_records_count() == 0:
                logger.info('No uncategorized digest records left')
                break

    def _print_non_categorized_digest_records_count(self):
        left_to_process_count = self._non_categorized_digest_records_count()
        print(f'Digest record(s) left to process: {left_to_process_count}')

    def _non_categorized_digest_records_count(self):
        url = f'{self.api_url}/not-categorized-digest-records-count/'
        response = self.get_with_retries(url=url,
                                         headers=self._auth_headers)
        response_str = response.content.decode()
        response_data = json.loads(response_str)
        return response_data['count']

    def _categorize_records_from_tbot(self):
        ignore_candidates_records = []
        for record in self.records:
            if not record.estimations:
                continue
            ignore_state_votes_count = len([estimation for estimation in record.estimations if estimation['state'] == DigestRecordState.IGNORED])
            ignore_vote_by_admin = len([estimation for estimation in record.estimations if estimation['user'] == 'gim6626' and estimation['state'] == DigestRecordState.IGNORED]) > 0  # TODO: Replace hardcode with some DB query on backend
            total_state_votes_count = len(record.estimations)
            if ignore_state_votes_count / total_state_votes_count > 0.5 and total_state_votes_count > 1 or ignore_vote_by_admin:
                ignore_candidates_records.append(record)
        if ignore_candidates_records:
            print('Candidates to ignore:')
            for ignore_candidate_record_i, ignore_candidate_record in enumerate(ignore_candidates_records):
                print(f'{ignore_candidate_record_i + 1}. {ignore_candidate_record.title} {ignore_candidate_record.url}')
            do_not_ignore_records_indexes = []
            while True:
                answer = input('Approve all records ignoring with typing "all" or comma-separated input records indexes which you want to left non-ignored: ')
                if answer == 'all':
                    do_not_ignore_records_indexes = []
                elif re.fullmatch(r'[0-9]+(,[0-9]+)*?', answer):
                    do_not_ignore_records_indexes = [int(i) - 1 for i in answer.split(',')]
                else:
                    print('Invalid answer, please input "all" or comma-separated indexes list')
                    continue
                break
            records_to_ignore = [ignore_candidate_record
                                 for ignore_candidate_record_i, ignore_candidate_record in enumerate(ignore_candidates_records)
                                 if ignore_candidate_record_i not in do_not_ignore_records_indexes]
            if records_to_ignore:
                logger.info('Setting following records as "ignored":')
                for record_to_ignore_i, record_to_ignore in enumerate(records_to_ignore):
                    logger.info(f'{record_to_ignore_i + 1}. {record_to_ignore.title} {record_to_ignore.url} {self._protocol}://{self._host}:{self._port}/admin/gatherer/digestrecord/{record_to_ignore.drid}/change/')
                    record_to_ignore.digest_issue = self._current_digest_issue
                    record_to_ignore.state = DigestRecordState.IGNORED
                logger.info('Uploading data')
                for record_to_ignore_i, record_to_ignore in enumerate(records_to_ignore):
                    self._upload_record(record_to_ignore)
            records_left_from_tbot = [digest_record
                                      for digest_record_i, digest_record in
                                      enumerate(ignore_candidates_records)
                                      if digest_record_i in do_not_ignore_records_indexes]
            self.records = records_left_from_tbot
        else:
            self.records = []

    def _categorize_new_records(self):
        self._filtered_records = []
        for record in self.records:
            if record.state == DigestRecordState.UNKNOWN:
                self._filtered_records.append(record)
                continue
            if record.is_main is None:
                self._filtered_records.append(record)
                continue
            if record.state == DigestRecordState.IN_DIGEST:
                if record.digest_issue is None:
                    self._filtered_records.append(record)
                    continue
                if record.category == DigestRecordCategory.UNKNOWN:
                    self._filtered_records.append(record)
                    continue
                if record.subcategory is None:
                    if record.category != DigestRecordCategory.OTHER:
                        self._filtered_records.append(record)
                        continue
        for record in self.records:
            # TODO: Rewrite using FSM
            logger.info(f'Processing record "{record.title}" from date {record.dt}')
            print(f'New record:\n{record}')
            self._show_similar_from_previous_digest(record.keywords)
            if record.state == DigestRecordState.UNKNOWN:
                record.state = self._ask_state(record)
            if record.digest_issue is None:
                record.digest_issue = self._current_digest_issue
            if record.state in (DigestRecordState.IN_DIGEST,
                                DigestRecordState.OUTDATED):
                if record.is_main is None:
                    is_main = self._ask_bool(f'Please input whether or no "{record.title}" is main (y/n): ')
                    logger.info(f'{"Marking" if is_main else "Not marking"} "{record.title}" as one of main records')
                    record.is_main = is_main

                guessed_category = self._guess_category(record.title, record.url)
                if guessed_category is not None:
                    msg = f'Guessed category is "{DIGEST_RECORD_CATEGORY_RU_MAPPING[guessed_category.value]}". Accept? y/n: '
                    accepted = self._ask_bool(msg)
                    if accepted:
                        logger.info(f'Setting category of record "{record.title}" to "{DIGEST_RECORD_CATEGORY_RU_MAPPING[guessed_category.value]}"')
                        record.category = guessed_category

                if guessed_category != DigestRecordCategory.OTHER:
                    guessed_subcategories, matched_subcategories_keywords = self._guess_subcategory(record.title)
                    if guessed_subcategories:
                        matched_subcategories_keywords_translated = {}
                        for matched_subcategory, keywords in matched_subcategories_keywords.items():
                            matched_subcategories_keywords_translated[DIGEST_RECORD_SUBCATEGORY_RU_MAPPING[matched_subcategory if matched_subcategory != 'databases' else 'db']] = keywords
                        print(f'Matched subcategories keywords:\n{pformat(matched_subcategories_keywords_translated)}')
                        if len(guessed_subcategories) == 1:
                            guessed_subcategory = guessed_subcategories[0]
                            msg = f'Guessed subcategory is "{DIGEST_RECORD_SUBCATEGORY_RU_MAPPING[guessed_subcategory.value]}". Accept? y/n: '
                            accepted = self._ask_bool(msg)
                            if accepted:
                                logger.info(f'Setting subcategory of record "{record.title}" to "{DIGEST_RECORD_SUBCATEGORY_RU_MAPPING[guessed_subcategory.value]}"')
                                record.subcategory = guessed_subcategory
                        else:
                            msg = f'Guessed subcategories are:'
                            for guessed_subcategory_i, guessed_subcategory in enumerate(guessed_subcategories):
                                msg += f'\n{guessed_subcategory_i + 1}. {DIGEST_RECORD_SUBCATEGORY_RU_MAPPING[guessed_subcategory.value]}'
                            msg += f'\nType guessed subcategory index or "n" if no match: '
                            guessed_subcategory_index = self._ask_guessed_subcategory_index(msg,
                                                                                            len(guessed_subcategories))
                            if guessed_subcategory_index is not None:
                                guessed_subcategory = guessed_subcategories[guessed_subcategory_index - 1]
                                logger.info(f'Setting subcategory of record "{record.title}" to "{DIGEST_RECORD_SUBCATEGORY_RU_MAPPING[guessed_subcategory.value]}"')
                                record.subcategory = guessed_subcategory

                if record.category == DigestRecordCategory.UNKNOWN or record.category is None:
                    record.category = self._ask_category(record,
                                                         DIGEST_RECORD_CATEGORY_RU_MAPPING)
                if record.subcategory is None:
                    if record.category != DigestRecordCategory.OTHER:
                        record.subcategory = self._ask_subcategory(record,
                                                                   DIGEST_RECORD_SUBCATEGORY_RU_MAPPING)

                if record.state == DigestRecordState.IN_DIGEST \
                        and record.category is not None \
                        and record.subcategory is not None:
                    current_records_with_similar_categories = self._similar_digest_records(record.digest_issue,
                                                                                           record.category,
                                                                                           record.subcategory)
                    if current_records_with_similar_categories:
                        print(f'Are there any duplicates for digest record "{record.title}" ({record.url})? Here is list of possible ones:')
                        i = 1
                        options_indexes = []
                        for option in current_records_with_similar_categories['duplicates']:
                            print(f'{i}. {"; ".join([dr["title"] + " " + dr["url"] for dr in option["digest_records"]])}')
                            options_indexes.append(option['id'])
                            i += 1
                        for option in current_records_with_similar_categories['records']:
                            print(f'{i}. {option["title"]} {option["url"]}')
                            options_indexes.append(option['id'])
                            i += 1
                        option_index = self._ask_option_index_or_no(i - 1)
                        print(Style.RESET_ALL, end='')
                        if option_index is not None:
                            if option_index <= len(current_records_with_similar_categories['duplicates']):
                                existing_drids = None
                                for option in current_records_with_similar_categories['duplicates']:
                                    if option['id'] == options_indexes[option_index - 1]:
                                        existing_drids = [dr['id'] for dr in option['digest_records']]
                                self._add_digest_record_do_duplicate(options_indexes[option_index - 1], existing_drids, record.drid)
                                logger.info('Added to duplicate')  # TODO: More details
                            else:
                                self._create_digest_record_duplicate(record.digest_issue, [options_indexes[option_index - 1], record.drid])
                                logger.info('New duplicate created')  # TODO: More details
                        else:
                            logger.info('No duplicates specified')
                    else:
                        logger.info('Similar digest records not found')

            self._upload_record(record)

    def _upload_record(self, record):
        logger.info(f'Uploading record #{record.drid} to FNGS')
        url = f'{self.api_url}/digest-records/{record.drid}/'
        data = json.dumps({
            'id': record.drid,
            'state': record.state.name if record.state is not None else None,
            'digest_issue': record.digest_issue,
            'is_main': record.is_main,
            'category': record.category.name if record.category is not None else None,
            'subcategory': record.subcategory.name if record.subcategory is not None else None,
        })
        response = self.patch_with_retries(url=url,
                                           headers=self._auth_headers,
                                           data=data)
        if response.status_code != 200:
            raise Exception(
                f'Invalid response code from FNGS patch - {response.status_code}: {response.content.decode("utf-8")}')
        logger.info(f'Uploaded record #{record.drid} for digest #{record.digest_issue} to FNGS')
        logger.info(f'If you want to change some parameters that you\'ve set - go to {self._protocol}://{self._host}:{self._port}/admin/gatherer/digestrecord/{record.drid}/change/')

    def _ask_state(self, record: DigestRecord):
        return self._ask_enum('digest record state', DigestRecordState, record)

    def _ask_option_index_or_no(self, max_index):
        while True:
            option_index_str = input(f'Please input option number or "n" to create new one: ')
            if option_index_str.isnumeric():
                option_index = int(option_index_str)
                if 0 < option_index <= max_index:
                    return option_index
                else:
                    logger.error(f'Index should be positive and not more than {max_index}')
            elif option_index_str == 'n':
                return None
            else:
                print('Invalid answer, it should be integer or "n"')
        raise NotImplementedError

    def _ask_digest_issue(self):
        while True:
            digest_issue_str = input(f'Please input current digest number: ')
            if digest_issue_str.isnumeric():
                digest_issue = int(digest_issue_str)
                return digest_issue
            else:
                print('Invalid digest number, it should be integer')
        raise NotImplementedError

    def _ask_guessed_subcategory_index(self,
                                       question: str,
                                       indexes_count: int):
        while True:
            answer = input(question)
            if answer.isnumeric():
                index = int(answer)
                if 1 <= index <= indexes_count:
                    return index
                else:
                    print(f'Invalid index, it should be between 1 and {indexes_count}')
                    continue
            elif answer == 'n':
                return None
            else:
                print(f'Invalid answer, it should be positive integer between 1 and {indexes_count} or "n"')
                continue
        raise NotImplementedError

    def _ask_bool(self, question: str):
        while True:
            bool_str = input(question)
            if bool_str == 'y':
                return True
            elif bool_str == 'n':
                return False
            else:
                print('Invalid boolean, it should be "y" or "n"')
        raise NotImplementedError

    def _ask_category(self,
                      record: DigestRecord,
                      translations: Dict[str, str] = None):
        return self._ask_enum('digest record category', DigestRecordCategory, record, translations)

    def _ask_subcategory(self,
                         record: DigestRecord,
                         translations: Dict[str, str] = None):
        enum_name = 'digest record subcategory'
        if record.category != DigestRecordCategory.UNKNOWN and record.category != DigestRecordCategory.OTHER:
            return self._ask_enum(enum_name, DigestRecordSubcategory, record, translations)
        else:
            raise NotImplementedError

    def _similar_digest_records(self,
                                digest_issue,
                                category,
                                subcategory):
        logger.debug(f'Getting similar records for digest number #{digest_issue}, category "{category.value}" and subcategory "{subcategory.value}"')
        url = f'{self.api_url}/similar-digest-records/?digest_issue={digest_issue}&category={category.name}&subcategory={subcategory.name}'
        response = self.get_with_retries(url, headers=self._auth_headers)
        if response.status_code != 200:
            logger.error(f'Failed to retrieve similar digest records, status code {response.status_code}, response: {response.content}')
            # TODO: Raise exception and handle above
            return None
        response_str = response.content.decode()
        response = json.loads(response_str)
        if not response:
            logger.info('No similar records found')
            return None
        options_duplicates = []
        options_records = []
        for similar_record_i, similar_record in enumerate(response):
            duplicates_object = self._duplicates_by_digest_record(similar_record['id'])
            if duplicates_object:
                options_duplicates.append(duplicates_object)
            else:
                options_records.append({'id': similar_record['id'], 'title': similar_record['title'], 'url': similar_record['url']})
        options_duplicates_filtered = []
        for option_duplicate in options_duplicates:
            duplicate_digest_records = []
            for duplicate_digest_record in option_duplicate['digest_records']:
                duplicate_digest_records.append({'id': duplicate_digest_record['id'], 'title': duplicate_digest_record['title'], 'url': duplicate_digest_record['url']})
            exists = False
            for option_duplicates_filtered in options_duplicates_filtered:
                if option_duplicates_filtered['id'] == option_duplicate['id']:
                    exists = True
                    break
            if not exists:
                options_duplicates_filtered.append({'id': option_duplicate['id'], 'digest_records': duplicate_digest_records})
        return {
            'duplicates': options_duplicates_filtered,
            'records': options_records,
        }

    def _add_digest_record_do_duplicate(self, duplicate_id, existing_drids, digest_record_id):
        logger.debug(f'Adding digest record #{digest_record_id} to duplicate #{duplicate_id}')
        url = f'{self.api_url}/digest-records-duplicates/{duplicate_id}/'
        data = {
            'id': duplicate_id,
            'digest_records': existing_drids + [digest_record_id],
        }
        response = self.patch_with_retries(url=url,
                                           data=json.dumps(data),
                                           headers=self._auth_headers)
        if response.status_code != 200:
            logger.error(f'Failed to update digest record duplicate, status code {response.status_code}, response: {response.content}')
            # TODO: Raise exception and handle above

    def _create_digest_record_duplicate(self,
                                        digest_issue,
                                        digest_records_ids,
                                        ):
        logger.debug(f'Creating digest record duplicate from #{digest_records_ids}')
        url = f'{self.api_url}/digest-records-duplicates/'
        data = {
            'digest_issue': digest_issue,
            'digest_records': digest_records_ids,
        }
        logger.debug(f'POSTing data {data} to URL {url}')
        response = self.post_with_retries(url,
                                          data=json.dumps(data),
                                          headers=self._auth_headers)
        if response.status_code != 201:
            logger.error(f'Failed to create digest record duplicate, status code {response.status_code}, response: {response.content}')
            # TODO: Raise exception and handle above

    def _digest_record_by_id(self, digest_record_id):
        logger.debug(f'Loading digest record #{digest_record_id}')
        url = f'{self.api_url}/digest-records/{digest_record_id}'
        response = self.get_with_retries(url, headers=self._auth_headers)
        if response.status_code != 200:
            logger.error(f'Failed to retrieve digest record, status code {response.status_code}, response: {response.content}')
            # TODO: Raise exception and handle above
            return None
        # logger.debug(f'Received response: {response.content}')  # TODO: Make "super debug" level and enable for it only
        response_str = response.content.decode()
        response = json.loads(response_str)
        if not response:
            # TODO: Raise exception and handle above
            logger.error('No digest record in response')
            return None
        return response

    def _duplicates_by_digest_record(self, digest_record_id):
        logger.debug(f'Checking if there are duplicates for digest record #{digest_record_id}')
        url = f'{self.api_url}/duplicates-by-digest-record/?digest_record={digest_record_id}'
        response = self.get_with_retries(url, headers=self._auth_headers)
        if response.status_code != 200:
            logger.error(f'Failed to retrieve similar digest records, status code {response.status_code}, response: {response.content}')
            # TODO: Raise exception and handle above
            return None
        # logger.debug(f'Received response: {response.content}')  # TODO: Make "super debug" level and enable for it only
        response_str = response.content.decode()
        response = json.loads(response_str)
        if not response:
            logger.debug(f'No duplicates found for digest record #{digest_record_id}')
            return None
        return response[0] # TODO: Handle multiple case

    def _ask_enum(self,
                  enum_name,
                  enum_class,
                  record: DigestRecord,
                  translations: Dict[str, str] = None):
        while True:
            logger.info(f'Waiting for {enum_name}')
            print(f'Available values:')
            enum_options_values = [enum_variant.value for enum_variant in enum_class]
            for enum_option_value_i, enum_option_value in enumerate(enum_options_values):
                enum_option_value_mod = translations[enum_option_value] if translations is not None else enum_option_value
                print(f'{enum_option_value_i + 1}. {enum_option_value_mod}')
            enum_value_index_str = input(f'Please input index of {enum_name} for "{record.title}": ')
            if enum_value_index_str.isnumeric():
                enum_value_index = int(enum_value_index_str)
                if 0 <= enum_value_index <= len(enum_options_values):
                    try:
                        enum_value = enum_options_values[enum_value_index - 1]
                        enum_obj = enum_class(enum_value)
                        enum_obj_value_mod = translations[enum_obj.value] if translations is not None else enum_obj.value
                        logger.info(f'Setting {enum_name} of record "{record.title}" to "{enum_obj_value_mod}"')
                        return enum_obj
                    except ValueError:
                        print(f'Invalid {enum_name} value "{enum_value}"')
                        continue
                else:
                    print(f'Invalid index, it should be positive and not more than {len(enum_options_values)}')
                    continue
            else:
                print('Invalid index, it should be integer')
                continue
        raise NotImplementedError
