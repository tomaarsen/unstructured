#!/usr/bin/env python3
import hashlib
import logging
import multiprocessing as mp
import sys
from contextlib import suppress
from pathlib import Path

import click

from unstructured.ingest.connector.github import GitHubConnector, SimpleGitHubConfig
from unstructured.ingest.connector.gitlab import GitLabConnector, SimpleGitLabConfig
from unstructured.ingest.connector.google_drive import (
    GoogleDriveConnector,
    SimpleGoogleDriveConfig,
)
from unstructured.ingest.connector.reddit import RedditConnector, SimpleRedditConfig
from unstructured.ingest.connector.s3 import S3Connector, SimpleS3Config
from unstructured.ingest.connector.wikipedia import (
    SimpleWikipediaConfig,
    WikipediaConnector,
)
from unstructured.ingest.doc_processor.generalized import initialize, process_document
from unstructured.ingest.logger import ingest_log_streaming_init, logger

with suppress(RuntimeError):
    mp.set_start_method("spawn")


class MainProcess:
    def __init__(
        self,
        doc_connector,
        doc_processor_fn,
        num_processes,
        reprocess,
        verbose,
    ):
        # initialize the reader and writer
        self.doc_connector = doc_connector
        self.doc_processor_fn = doc_processor_fn
        self.num_processes = num_processes
        self.reprocess = reprocess
        self.verbose = verbose

    def initialize(self):
        """Slower initialization things: check connections, load things into memory, etc."""
        ingest_log_streaming_init(logging.DEBUG if self.verbose else logging.INFO)
        self.doc_connector.initialize()
        initialize()

    def cleanup(self):
        self.doc_connector.cleanup()

    def _filter_docs_with_outputs(self, docs):
        num_docs_all = len(docs)
        docs = [doc for doc in docs if not doc.has_output()]
        num_docs_to_process = len(docs)
        if num_docs_to_process == 0:
            logger.info(
                "All docs have structured outputs, nothing to do. Use --reprocess to process all.",
            )
            return None
        elif num_docs_to_process != num_docs_all:
            logger.info(
                f"Skipping processing for {num_docs_all - num_docs_to_process} docs out of "
                f"{num_docs_all} since their structured outputs already exist, use --reprocess to "
                "reprocess those in addition to the unprocessed ones.",
            )
        return docs

    def run(self):
        self.initialize()

        # fetch the list of lazy downloading IngestDoc obj's
        docs = self.doc_connector.get_ingest_docs()

        # remove docs that have already been processed
        if not self.reprocess:
            docs = self._filter_docs_with_outputs(docs)
            if not docs:
                return

        # Debugging tip: use the below line and comment out the mp.Pool loop
        # block to remain in single process
        # self.doc_processor_fn(docs[0])
        with mp.Pool(
            processes=self.num_processes,
            initializer=ingest_log_streaming_init,
            initargs=(logging.DEBUG if self.verbose else logging.INFO,),
        ) as pool:
            pool.map(self.doc_processor_fn, docs)

        self.cleanup()


@click.command()
@click.option(
    "--s3-url",
    default=None,
    help="Prefix of s3 objects (files) to download. E.g. s3://bucket1/path/. This value may "
    "also be a single file.",
)
@click.option(
    "--s3-anonymous",
    is_flag=True,
    default=False,
    help="Connect to s3 without local AWS credentials.",
)
@click.option(
    "--drive-id",
    default=None,
    help="Google Drive File or Folder ID.",
)
@click.option(
    "--drive-service-account-key",
    default=None,
    help="Path to the Google Drive service account json file.",
)
@click.option(
    "--drive-recursive",
    is_flag=True,
    default=False,
    help="Recursively download files in folders from the Google Drive ID, "
    "otherwise stop at the files in provided folder level.",
)
@click.option(
    "--drive-extension",
    default=None,
    help="Filters the files to be processed based on extension e.g. .jpg, .docx, etc.",
)
@click.option(
    "--wikipedia-page-title",
    default=None,
    help='Title of a Wikipedia page, e.g. "Open source software".',
)
@click.option(
    "--wikipedia-auto-suggest",
    default=True,
    help="Whether to automatically suggest a page if the exact page was not found."
    " Set to False if the wrong Wikipedia page is fetched.",
)
@click.option(
    "--github-url",
    default=None,
    help='URL to GitHub repository, e.g. "https://github.com/Unstructured-IO/unstructured",'
    ' or a repository owner/name pair, e.g. "Unstructured-IO/unstructured"',
)
@click.option(
    "--gitlab-url",
    default=None,
    help='URL to GitLab repository, e.g. "https://gitlab.com/gitlab-com/content-sites/docsy-gitlab"'
    ', or a repository path, e.g. "gitlab-com/content-sites/docsy-gitlab"',
)
@click.option(
    "--git-access-token",
    default=None,
    help="A GitHub or GitLab access token, see https://docs.github.com/en/authentication "
    " or https://docs.gitlab.com/ee/api/rest/index.html#personalprojectgroup-access-tokens",
)
@click.option(
    "--git-branch",
    default=None,
    help="The branch for which to fetch files from. If not given,"
    " the default repository branch is used.",
)
@click.option(
    "--git-file-glob",
    default=None,
    help="A comma-separated list of file globs to limit which types of files are accepted,"
    " e.g. '*.html,*.txt'",
)
@click.option(
    "--subreddit-name",
    default=None,
    help='The name of a subreddit, without the "r\\", e.g. "machinelearning"',
)
@click.option(
    "--reddit-client-id",
    default=None,
    help="The client ID, see "
    "https://praw.readthedocs.io/en/stable/getting_started/quick_start.html#prerequisites"
    " for more information.",
)
@click.option(
    "--reddit-client-secret",
    default=None,
    help="The client secret, see "
    "https://praw.readthedocs.io/en/stable/getting_started/quick_start.html#prerequisites"
    " for more information.",
)
@click.option(
    "--reddit-user-agent",
    default="Unstructured Ingest Subreddit fetcher",
    help="The user agent to use on the Reddit API, see "
    "https://praw.readthedocs.io/en/stable/getting_started/quick_start.html#prerequisites"
    " for more information.",
)
@click.option(
    "--reddit-search-query",
    default=None,
    help="If set, return posts using this query. Otherwise, use hot posts.",
)
@click.option("--reddit-num-posts", default=10, help="The number of posts to fetch.")
@click.option(
    "--re-download/--no-re-download",
    default=False,
    help="Re-download files even if they are already present in --download-dir.",
)
@click.option(
    "--download-dir",
    help="Where files are downloaded to, defaults to `$HOME/.cache/unstructured/ingest/<SHA256>`.",
)
@click.option(
    "--preserve-downloads",
    is_flag=True,
    default=False,
    help="Preserve downloaded files. Otherwise each file is removed after being processed "
    "successfully.",
)
@click.option(
    "--structured-output-dir",
    default="structured-output",
    help="Where to place structured output .json files.",
)
@click.option(
    "--reprocess",
    is_flag=True,
    default=False,
    help="Reprocess a downloaded file even if the relevant structured output .json file "
    "in --structured-output-dir already exists.",
)
@click.option(
    "--num-processes",
    default=2,
    show_default=True,
    help="Number of parallel processes to process docs in.",
)
@click.option("-v", "--verbose", is_flag=True, default=False)
def main(
    s3_url,
    drive_id,
    drive_service_account_key,
    drive_recursive,
    drive_extension,
    wikipedia_page_title,
    wikipedia_auto_suggest,
    github_url,
    gitlab_url,
    git_access_token,
    git_branch,
    git_file_glob,
    subreddit_name,
    reddit_client_id,
    reddit_client_secret,
    reddit_user_agent,
    reddit_search_query,
    reddit_num_posts,
    re_download,
    download_dir,
    preserve_downloads,
    structured_output_dir,
    reprocess,
    num_processes,
    s3_anonymous,
    verbose,
):
    if not preserve_downloads and download_dir:
        logger.warning(
            "Not preserving downloaded files but --download_dir is specified",
        )
    if not download_dir:
        cache_path = Path.home() / ".cache" / "unstructured" / "ingest"
        if not cache_path.exists():
            cache_path.mkdir(parents=True, exist_ok=True)
        if s3_url:
            hashed_dir_name = hashlib.sha256(s3_url.encode("utf-8"))
        elif github_url:
            hashed_dir_name = hashlib.sha256(
                f"{github_url}_{git_branch}".encode("utf-8"),
            )
        elif gitlab_url:
            hashed_dir_name = hashlib.sha256(
                f"{gitlab_url}_{git_branch}".encode("utf-8"),
            )
        elif subreddit_name:
            hashed_dir_name = hashlib.sha256(
                subreddit_name.encode("utf-8"),
            )
        elif wikipedia_page_title:
            hashed_dir_name = hashlib.sha256(
                wikipedia_page_title.encode("utf-8"),
            )
        elif drive_id:
            hashed_dir_name = hashlib.sha256(
                drive_id.encode("utf-8"),
            )
        else:
            raise ValueError("No connector-specific option was specified!")
        download_dir = cache_path / hashed_dir_name.hexdigest()[:10]
        if preserve_downloads:
            logger.warning(
                f"Preserving downloaded files but --download-dir is not specified,"
                f" using {download_dir}",
            )
    if s3_url:
        doc_connector = S3Connector(
            config=SimpleS3Config(
                path=s3_url,
                access_kwargs={"anon": s3_anonymous},
                download_dir=download_dir,
                output_dir=structured_output_dir,
                re_download=re_download,
                preserve_downloads=preserve_downloads,
            ),
        )
    elif github_url:
        doc_connector = GitHubConnector(  # type: ignore
            config=SimpleGitHubConfig(
                url=github_url,
                access_token=git_access_token,
                branch=git_branch,
                file_glob=git_file_glob,
                # defaults params:
                download_dir=download_dir,
                preserve_downloads=preserve_downloads,
                output_dir=structured_output_dir,
                re_download=re_download,
            ),
        )
    elif gitlab_url:
        doc_connector = GitLabConnector(  # type: ignore
            config=SimpleGitLabConfig(
                url=gitlab_url,
                access_token=git_access_token,
                branch=git_branch,
                file_glob=git_file_glob,
                # defaults params:
                download_dir=download_dir,
                preserve_downloads=preserve_downloads,
                output_dir=structured_output_dir,
                re_download=re_download,
            ),
        )
    elif subreddit_name:
        doc_connector = RedditConnector(  # type: ignore
            config=SimpleRedditConfig(
                subreddit_name=subreddit_name,
                client_id=reddit_client_id,
                client_secret=reddit_client_secret,
                user_agent=reddit_user_agent,
                search_query=reddit_search_query,
                num_posts=reddit_num_posts,
                # defaults params:
                download_dir=download_dir,
                preserve_downloads=preserve_downloads,
                output_dir=structured_output_dir,
                re_download=re_download,
            ),
        )
    elif wikipedia_page_title:
        doc_connector = WikipediaConnector(  # type: ignore
            config=SimpleWikipediaConfig(
                title=wikipedia_page_title,
                auto_suggest=wikipedia_auto_suggest,
                # defaults params:
                download_dir=download_dir,
                preserve_downloads=preserve_downloads,
                output_dir=structured_output_dir,
                re_download=re_download,
            ),
        )
    elif drive_id:
        doc_connector = GoogleDriveConnector(  # type: ignore
            config=SimpleGoogleDriveConfig(
                drive_id=drive_id,
                service_account_key=drive_service_account_key,
                recursive=drive_recursive,
                extension=drive_extension,
                # defaults params:
                download_dir=download_dir,
                preserve_downloads=preserve_downloads,
                output_dir=structured_output_dir,
                re_download=re_download,
            ),
        )
    # Check for other connector-specific options here and define the doc_connector object
    # e.g. "elif azure_container:  ..."

    else:
        logger.error("No connector-specific option was specified!")
        sys.exit(1)

    MainProcess(
        doc_connector=doc_connector,
        doc_processor_fn=process_document,
        num_processes=num_processes,
        reprocess=reprocess,
        verbose=verbose,
    ).run()


if __name__ == "__main__":
    main()
