import colorama
from aiohttp import (
    ClientConnectorError,
    ClientOSError,
    ClientResponseError,
    ClientSession,
    ServerDisconnectedError,
)

from zucaro.errors import AuthenticationError, RefreshError, ValidationError
from zucaro.logging import logger

URL_DEVICE_AUTH = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
URL_TOKEN = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
URL_XBL = "https://user.auth.xboxlive.com/user/authenticate"
URL_XSTS = "https://xsts.auth.xboxlive.com/xsts/authorize"
URL_MCS = "https://api.minecraftservices.com/authentication/login_with_xbox"
URL_MCS_PROFILE = "https://api.minecraftservices.com/minecraft/profile"

CLIENT_ID = "c52aed44-3b4d-4215-99c5-824033d2bc0f"
SCOPE = "XboxLive.signin offline_access"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


class MicrosoftAuthApi:
    async def _ms_oauth(self):
        data = {"client_id": CLIENT_ID, "scope": SCOPE}

        try:
            async with ClientSession() as session:
                async with session.post(URL_DEVICE_AUTH, data=data) as resp:
                    resp.raise_for_status()
                    j = await resp.json()
        except (ClientConnectorError, ServerDisconnectedError, ClientOSError) as e:
            raise AuthenticationError("Connection error during OAuth", e)

        device_code = j["device_code"]

        msg = j["message"]
        user_code = j["user_code"]
        link = j["verification_uri"]

        msg = msg.replace(
            user_code, colorama.Fore.RED + user_code + colorama.Fore.RESET
        ).replace(link, colorama.Style.BRIGHT + link + colorama.Style.NORMAL)

        logger.info(msg)

        data = {"code": device_code, "grant_type": GRANT_TYPE, "client_id": CLIENT_ID}

        first = True
        while True:
            if first:
                input("Press enter to continue... ")
            else:
                input("Press enter to try again... ")
            first = False

            try:
                async with ClientSession() as session:
                    async with session.post(URL_TOKEN, data=data) as resp:
                        if resp.status == 400:
                            j = await resp.json()
                            logger.debug(j)
                            if j["error"] == "authorization_pending":
                                logger.warning(j["error_description"])
                                logger.info(msg)
                                continue
                            else:
                                raise AuthenticationError(j["error_description"])
                        resp.raise_for_status()
                        j = await resp.json()
                        break
            except (ClientConnectorError, ServerDisconnectedError, ClientOSError) as e:
                raise AuthenticationError("Connection error during token polling", e)

        access_token = j["access_token"]
        refresh_token = j["refresh_token"]
        logger.debug("OAuth device code flow successful")
        return access_token, refresh_token

    async def _ms_oauth_refresh(self, refresh_token):
        data = {
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
        }
        try:
            async with ClientSession() as session:
                async with session.post(URL_TOKEN, data=data) as resp:
                    resp.raise_for_status()
                    j = await resp.json()
        except (ClientConnectorError, ServerDisconnectedError, ClientOSError) as e:
            raise RefreshError("Connection error during token refresh", e)

        access_token = j["access_token"]
        refresh_token = j["refresh_token"]
        logger.debug("OAuth code flow refresh successful")
        return access_token, refresh_token

    async def _xbl_auth(self, access_token):
        data = {
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": f"d={access_token}",
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
        }
        try:
            async with ClientSession() as session:
                async with session.post(URL_XBL, json=data) as resp:
                    resp.raise_for_status()
                    j = await resp.json()
        except (ClientConnectorError, ServerDisconnectedError, ClientOSError) as e:
            raise AuthenticationError("Connection error during XBL auth", e)

        logger.debug("XBL auth successful")
        return j["Token"], j["DisplayClaims"]["xui"][0]["uhs"]

    async def _xsts_auth(self, xbl_token):
        data = {
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
            "RelyingParty": "rp://api.minecraftservices.com/",
            "TokenType": "JWT",
        }
        try:
            async with ClientSession() as session:
                async with session.post(URL_XSTS, json=data) as resp:
                    resp.raise_for_status()
                    j = await resp.json()
        except (ClientConnectorError, ServerDisconnectedError, ClientOSError) as e:
            raise AuthenticationError("Connection error during XSTS auth", e)

        logger.debug("XSTS auth successful")
        return j["Token"]

    async def _mcs_auth(self, uhs, xsts_token):
        data = {"identityToken": f"XBL3.0 x={uhs};{xsts_token}"}
        try:
            async with ClientSession() as session:
                async with session.post(URL_MCS, json=data) as resp:
                    resp.raise_for_status()
                    j = await resp.json()
        except (ClientConnectorError, ServerDisconnectedError, ClientOSError) as e:
            raise AuthenticationError("Connection error during Minecraft services auth", e)

        logger.debug("Minecraft services auth successful")
        return j["access_token"]

    async def get_profile(self, mc_access_token):
        try:
            async with ClientSession() as session:
                async with session.get(
                    URL_MCS_PROFILE, headers={"Authorization": f"Bearer {mc_access_token}"}
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except ClientResponseError as e:
            raise AuthenticationError(e)
        except (ClientConnectorError, ServerDisconnectedError, ClientOSError) as e:
            raise AuthenticationError("Connection error getting profile", e)

    async def _auth_rest(self, access_token, refresh_token):
        xbl_token, uhs = await self._xbl_auth(access_token)
        xsts_token = await self._xsts_auth(xbl_token)
        mc_access_token = await self._mcs_auth(uhs, xsts_token)
        return mc_access_token

    async def authenticate(self):
        try:
            access_token, refresh_token = await self._ms_oauth()
            mc_access_token = await self._auth_rest(access_token, refresh_token)
            return mc_access_token, refresh_token
        except ClientResponseError as e:
            raise AuthenticationError(e)
        except KeyError as e:
            raise AuthenticationError("Missing field in response", e)

    async def validate(self, mc_access_token):
        try:
            async with ClientSession() as session:
                async with session.get(
                    URL_MCS_PROFILE, headers={"Authorization": f"Bearer {mc_access_token}"}
                ) as resp:
                    if resp.status == 401:
                        return False

                    resp.raise_for_status()
                    profile = await resp.json()

                    return "id" in profile
        except ClientResponseError as e:
            raise ValidationError(e)
        except (ClientConnectorError, ServerDisconnectedError, ClientOSError):
            return False

    async def refresh(self, refresh_token):
        try:
            access_token, new_refresh_token = await self._ms_oauth_refresh(refresh_token)
            mc_access_token = await self._auth_rest(access_token, new_refresh_token)
            return mc_access_token, new_refresh_token
        except ClientResponseError as e:
            raise RefreshError(e)
