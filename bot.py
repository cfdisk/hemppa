#!/usr/bin/env python3

import asyncio
import glob
import importlib
import json
import os
import re
import sys
import traceback
import urllib.parse

import requests
from nio import AsyncClient, InviteEvent, JoinError, RoomMessageText
from importlib import reload

# Couple of custom exceptions


class CommandRequiresAdmin(Exception):
    pass


class CommandRequiresOwner(Exception):
    pass


class Bot:
    appid = 'org.vranki.hemppa'
    version = '1.2'
    client = None
    join_on_invite = False
    modules = dict()
    pollcount = 0
    poll_task = None
    owners = []

    async def send_text(self, room, body):
        msg = {
            "body": body,
            "msgtype": "m.text"
        }
        await self.client.room_send(room.room_id, 'm.room.message', msg)

    async def send_html(self, room, html, plaintext):
        msg = {
            "msgtype": "m.text",
            "format": "org.matrix.custom.html",
            "formatted_body": html,
            "body": plaintext
        }
        await self.client.room_send(room.room_id, 'm.room.message', msg)

    def get_room_by_id(self, room_id):
        return self.client.rooms[room_id]

    # Throws exception if event sender is not a room admin
    def must_be_admin(self, room, event):
        if not self.is_admin(room, event):
            raise CommandRequiresAdmin

    # Throws exception if event sender is not a bot owner
    def must_be_owner(self, event):
        if not self.is_owner(event):
            raise CommandRequiresOwner

    # Returns true if event's sender is admin in the room event was sent in,
    # or is bot owner
    def is_admin(self, room, event):
        if self.is_owner(event):
            return True
        if event.sender not in room.power_levels.users:
            return False
        return room.power_levels.users[event.sender] >= 50

    # Returns true if event's sender is owner of the bot
    def is_owner(self, event):
        return event.sender in self.owners

    def save_settings(self):
        module_settings = dict()
        for modulename, moduleobject in self.modules.items():
            if "get_settings" in dir(moduleobject):
                try:
                    module_settings[modulename] = moduleobject.get_settings()
                except Exception:
                    traceback.print_exc(file=sys.stderr)
        data = {self.appid: self.version, 'module_settings': module_settings}
        self.set_account_data(data)

    def load_settings(self, data):
        if not data:
            return
        if not data.get('module_settings'):
            return
        for modulename, moduleobject in self.modules.items():
            if data['module_settings'].get(modulename):
                if "set_settings" in dir(moduleobject):
                    try:
                        moduleobject.set_settings(
                            data['module_settings'][modulename])
                    except Exception:
                        traceback.print_exc(file=sys.stderr)

    async def message_cb(self, room, event):
        # Figure out the command
        body = event.body
        if len(body) == 0:
            return
        if body[0] != '!':
            return

        command = body.split().pop(0)

        # Strip away non-alphanumeric characters, including leading ! for security
        command = re.sub(r'\W+', '', command)

        moduleobject = self.modules.get(command)

        if "matrix_message" in dir(moduleobject):
            try:
                await moduleobject.matrix_message(bot, room, event)
            except CommandRequiresAdmin:
                await self.send_text(room, f'Sorry, you need admin power level in this room to run that command.')
            except CommandRequiresOwner:
                await self.send_text(room, f'Sorry, only bot owner can run that command.')
            except Exception:
                await self.send_text(room, f'Module {command} experienced difficulty: {sys.exc_info()[0]} - see log for details')
                traceback.print_exc(file=sys.stderr)

    async def invite_cb(self, room, event):
        if self.join_on_invite or self.is_owner(event):
            for attempt in range(3):
                result = await self.client.join(room.room_id)
                if type(result) == JoinError:
                    print(f"Error joining room {room.room_id} (attempt %d): %s",
                          attempt, result.message,
                          )
                else:
                    break
        else:
            print(
                f'Received invite event, but not joining as sender is not owner or bot not configured to join on invite. {event}')

    def load_module(self, modulename):
        try:
            module = importlib.import_module('modules.' + modulename)
            module = reload(module)
            cls = getattr(module, 'MatrixModule')
            return cls()
        except ModuleNotFoundError:
            print('Module ', modulename, ' failed to load!')
            traceback.print_exc(file=sys.stderr)
            return None

    def reload_modules(self):
        for modulename in bot.modules:
            print('Reloading', modulename, '..')
            self.modules[modulename] = self.load_module(modulename)

        self.load_settings(self.get_account_data())

    def get_modules(self):
        modulefiles = glob.glob('./modules/*.py')

        for modulefile in modulefiles:
            modulename = os.path.splitext(os.path.basename(modulefile))[0]
            moduleobject = self.load_module(modulename)
            if moduleobject:
                self.modules[modulename] = moduleobject
    
    def clear_modules(self):
        self.modules = dict()

    async def poll_timer(self):
        while True:
            self.pollcount = self.pollcount + 1
            for modulename, moduleobject in self.modules.items():
                if "matrix_poll" in dir(moduleobject):
                    try:
                        await moduleobject.matrix_poll(bot, self.pollcount)
                    except Exception:
                        traceback.print_exc(file=sys.stderr)
            await asyncio.sleep(10)

    def set_account_data(self, data):
        userid = urllib.parse.quote(os.environ['MATRIX_USER'])

        ad_url = f"{self.client.homeserver}/_matrix/client/r0/user/{userid}/account_data/{self.appid}?access_token={self.client.access_token}"

        response = requests.put(ad_url, json.dumps(data))
        if response.status_code != 200:
            print('Setting account data failed:', response, response.json())

    def get_account_data(self):
        userid = urllib.parse.quote(os.environ['MATRIX_USER'])

        ad_url = f"{self.client.homeserver}/_matrix/client/r0/user/{userid}/account_data/{self.appid}?access_token={self.client.access_token}"

        response = requests.get(ad_url)
        if response.status_code == 200:
            return response.json()
        print(
            f'Getting account data failed: {response} {response.json()} - this is normal if you have not saved any settings yet.')
        return None

    def init(self):
        self.client = AsyncClient(
            os.environ['MATRIX_SERVER'], os.environ['MATRIX_USER'])
        self.client.access_token = os.getenv('MATRIX_ACCESS_TOKEN')
        self.join_on_invite = os.getenv("JOIN_ON_INVITE") is not None
        self.owners = os.environ['BOT_OWNERS'].split(',')
        self.get_modules()

    def start(self):
        print(f'Starting {len(self.modules)} modules..')
        for modulename, moduleobject in self.modules.items():
            print('Starting', modulename, '..')
            if "matrix_start" in dir(moduleobject):
                try:
                    moduleobject.matrix_start(bot)
                except Exception:
                    traceback.print_exc(file=sys.stderr)

    def stop(self):
        print(f'Stopping {len(self.modules)} modules..')
        for modulename, moduleobject in self.modules.items():
            print('Stopping', modulename, '..')
            if "matrix_stop" in dir(moduleobject):
                try:
                    moduleobject.matrix_stop(bot)
                except Exception:
                    traceback.print_exc(file=sys.stderr)

    async def run(self):
        if not self.client.access_token:
            await self.client.login(os.environ['MATRIX_PASSWORD'])
            print("Logged in with password, access token:",
                  self.client.access_token)

        await self.client.sync()
        for roomid in self.client.rooms:
            print(f'Bot is on {roomid} with {len(self.client.rooms[roomid].users)} users')
            if len(self.client.rooms[roomid].users) == 1:
                print(f'Room {roomid} has no other users - leaving it.')
                print(await self.client.room_leave(roomid))

        self.start()

        self.poll_task = asyncio.get_event_loop().create_task(self.poll_timer())

        if self.client.logged_in:
            self.load_settings(self.get_account_data())
            self.client.add_event_callback(self.message_cb, RoomMessageText)
            self.client.add_event_callback(self.invite_cb, (InviteEvent,))

            if self.join_on_invite:
                print('Note: Bot will join rooms if invited')
            print('Bot running as', self.client.user, ', owners', self.owners)
            self.bot_task = asyncio.create_task(self.client.sync_forever(timeout=30000))
            await self.bot_task
        else:
            print('Client was not able to log in, check env variables!')


bot = Bot()
bot.init()
try:
    asyncio.get_event_loop().run_until_complete(bot.run())
except KeyboardInterrupt:
    if bot.poll_task:
        bot.poll_task.cancel()
    bot.bot_task.cancel()

bot.stop()
