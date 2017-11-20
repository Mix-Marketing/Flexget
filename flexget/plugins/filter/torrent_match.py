from __future__ import unicode_literals, division, absolute_import
from builtins import *  # noqa pylint: disable=unused-import, redefined-builtin

import logging
import os

from flexget import plugin
from flexget.event import event
from flexget.plugin import get_plugin_by_name, PluginError

log = logging.getLogger('torrent_match')


class TorrentMatchFile(object):
    def __init__(self, path, size):
        self.path = path
        self.size = size

    def __repr__(self):
        return "%s(path=%s, size=%s)" % (self.__class__.__name__, str(self.path), self.size)


class TorrentMatch(object):
    """Plugin that attempts to match .torrents to local files"""

    schema = {
        'type': 'object',
        'properties': {
            'what': {'type': 'array', 'items': {
                'allOf': [{'$ref': '/schema/plugins?phase=input'}, {'maxProperties': 1, 'minProperties': 1}]
            }},
            'max_size_difference': {'type': 'string', 'format': 'percent', 'default': '0%'}
        },
        'required': ['what'],
        'additionalProperties': False
    }

    def execute_inputs(self, config, task):
        """
        :param config: TorrentMatch config
        :param task: Current task
        :return: List of pseudo entries created by inputs under `what` configuration
        """
        entries = set()
        entry_urls = set()
        # run inputs
        for item in config['what']:
            for input_name, input_config in item.items():
                input = get_plugin_by_name(input_name)
                if input.api_ver == 1:
                    raise PluginError('Plugin %s does not support API v2' % input_name)
                method = input.phase_handlers['input']
                try:
                    result = method(task, input_config)
                except PluginError as e:
                    log.warning('Error during input plugin %s: %s', input_name, e)
                    continue
                if not result:
                    log.warning('Input %s did not return anything', input_name)
                    continue

                for entry in result:
                    urls = ([entry['url']] if entry.get('url') else []) + entry.get('urls', [])
                    if any(url in entry_urls for url in urls):
                        log.debug('URL for `%s` already in entry list, skipping.', entry['title'])
                        continue

                    entries.add(entry)
                    entry_urls.update(urls)
        return entries

    def get_local_files(self, config, task):
        cwd = os.getcwd()  # save the current working directory
        entries = self.execute_inputs(config, task)
        for entry in entries:
            location = entry.get('location')
            if not location or not os.path.exists(location):
                log.warning('%s is not a local file. Skipping.', entry['title'])
                entry.reject('not a local file')
                continue

            entry['files'] = []

            if os.path.isfile(location):
                entry['files'].append(TorrentMatchFile(location, os.path.getsize(location)))
            else:
                # change working dir to make things simpler
                os.chdir(location)
                # traverse the file tree
                for root, _, files in os.walk('.'):
                    # we only need to iterate over files
                    for f in files:
                        file_path = os.path.join(root, f)
                        # We need normpath to strip out the dot
                        abs_file_path = os.path.normpath(os.path.join(location, file_path))
                        entry['files'].append(TorrentMatchFile(abs_file_path, os.path.getsize(file_path)))

        # restore the working directory
        os.chdir(cwd)

        return entries

    # Run last in download phase to make sure we have downloaded .torrent to temp before modify phase
    @plugin.priority(0)
    def on_task_download(self, task, config):
        for entry in task.accepted:
            if 'file' not in entry and 'download' not in task.config:
                # If the download plugin is not enabled, we need to call it to get
                # our temp .torrent files
                download = plugin.get_plugin_by_name('download')
                download.instance.get_temp_files(task, handle_magnets=True, fail_html=True)

    def prepare_config(self, config):
        if not isinstance(config['max_size_difference'], float):
            config['max_size_difference'] = float(config['max_size_difference'].rstrip('%'))

        return config

    # Run after 'torrent' plugin, this is not really a modify plugin though, but we need 'torrent' field
    def on_task_modify(self, task, config):
        config = self.prepare_config(config)
        max_size_difference = config['max_size_difference']
        local_entries = self.get_local_files(config, task)

        matched_entries = set()
        for entry in task.accepted:
            if 'torrent' not in entry:
                log.debug('Skipping entry %s as it is not a torrent file', entry['title'])
                continue

            # Find all files and file sizes in the .torrent.
            torrent_files = []
            for item in entry['torrent'].get_filelist():
                # if torrent is a multi_file, prepend the name
                path = os.path.join(item['path'], item['name'])
                if entry['torrent'].is_multi_file:
                    path = os.path.join(entry['torrent'].name, path)

                torrent_files.append(TorrentMatchFile(path, item['size']))

            # Iterate over the files/dirs from the  what  plugins
            for local_entry in local_entries:
                log.debug("Checking local entry %s against %s", local_entry['title'], entry['title'])

                local_files = local_entry['files']

                # skip root dir of the local entry if torrent is single file
                has_root_dir = entry['torrent'].is_multi_file and entry['torrent'].name

                if not has_root_dir:  # single-file
                    torrent_file = torrent_files[0]
                    for local_file in local_files:
                        if torrent_file.path in local_file.path and torrent_file.size == local_file.size:
                            # if the filename with ext is contained in 'location', we must grab its parent as path
                            if os.path.basename(torrent_file.path) in local_entry['location']:
                                entry['path'] = os.path.dirname(local_entry['location'])
                            else:
                                entry['path'] = local_entry['location']
                            matched_entries.add(entry)
                            break
                else:
                    matches = 0
                    missing_size = 0
                    total_size = 0
                    path = ''

                    candidate_files = []
                    # Find candidate files ie. files whose path contains the torrent name
                    for local_file in local_files:
                        if entry['torrent'].name in local_file.path:
                            # we need to find the path that contains the torrent name since it's multi-file
                            if not path:
                                # attempt to extract path from the absolute file path
                                path = local_file.path

                                while entry['torrent'].name in path:
                                    path = os.path.dirname(path)

                            candidate_files.append(local_file)

                    for torrent_file in torrent_files:
                        for candidate in candidate_files:
                            if torrent_file.path in candidate.path and torrent_file.size == candidate.size:
                                matches += 1
                                break
                        else:
                            missing_size += torrent_file.size
                        total_size += torrent_file.size

                    size_difference = missing_size / total_size * 100
                    # we allow torrents that either match entirely or if the total size difference is below a threshold
                    if matches == len(torrent_files) or max_size_difference >= size_difference:
                        matched_entries.add(entry)
                        # set the path of the torrent entry
                        entry['path'] = path

                        log.debug('Torrent %s matched path %s', entry['title'], entry['path'])
                        # TODO keep searching for even better matches?
                        break

        for entry in set(task.accepted).difference(matched_entries):
            entry.reject('No local files matched {}% of the torrent size'.format(100 - max_size_difference))


@event('plugin.register')
def register_plugin():
    plugin.register(TorrentMatch, 'torrent_match', api_ver=2)
