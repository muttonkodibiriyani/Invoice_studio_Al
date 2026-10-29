"""Official Sign in with ChatGPT flow for an open-source, loopback-hosted app.

Tokens stay encrypted on the server. This module never uses ChatGPT backend-api
endpoints or copies credentials from another application.
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from urllib.parse import urlencode, urlparse
import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken

AUTH="https://auth.openai.com"
RESOURCE="https://api.openai.com/v1"
SCOPES="openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
BUNDLE_FORMAT="inv-studio.chatgpt-registration"
SEALED_FORMAT="inv-studio.chatgpt-registration.sealed"
MAX_BUNDLE_BYTES=300_000
MAX_TOKEN_BYTES=100_000


def _mapping(value,required,allowed):
    if not isinstance(value,dict) or not required.issubset(value) or not set(value).issubset(allowed):
        raise ValueError("invalid credential bundle")
    return value


def _text(value,minimum=1,maximum=MAX_TOKEN_BYTES):
    if not isinstance(value,str) or not minimum<=len(value)<=maximum:
        raise ValueError("invalid credential bundle")
    if any(ord(char)<0x21 or ord(char)>0x7e for char in value):
        raise ValueError("invalid credential bundle")
    return value


def _timestamp(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or value<=0:
        raise ValueError("invalid credential bundle")
    return float(value)


def _display_text(value,maximum):
    if not isinstance(value,str) or not 1<=len(value)<=maximum:
        raise ValueError("invalid credential bundle")
    if any(ord(char)<0x20 or ord(char)==0x7f for char in value):
        raise ValueError("invalid credential bundle")
    return value


def _scope_list(value):
    if not isinstance(value,list) or not 1<=len(value)<=32:
        raise ValueError("invalid credential bundle")
    scopes=[_text(item,1,100) for item in value]
    if len(set(scopes))!=len(scopes):
        raise ValueError("invalid credential bundle")
    return scopes


def _identity_keys(http):
    config=http.get(AUTH+"/.well-known/openid-configuration")
    config.raise_for_status()
    config_body=config.json()
    if not isinstance(config_body,dict):raise ValueError("invalid identity configuration")
    jwks_url=config_body.get("jwks_uri","")
    parsed=urlparse(jwks_url)
    if parsed.scheme!="https" or parsed.hostname!="auth.openai.com":
        raise ValueError("unexpected identity key endpoint")
    response=http.get(jwks_url);response.raise_for_status()
    body=response.json()
    if not isinstance(body,dict) or not isinstance(body.get("keys"),list):
        raise ValueError("invalid identity keys")
    return body["keys"]


def _verified_jwt(token,keys,audience,required):
    header=jwt.get_unverified_header(token)
    if header.get("alg")!="RS256" or not isinstance(header.get("kid"),str):
        raise ValueError("unexpected token signing algorithm")
    key=next((item for item in keys if item.get("kid")==header["kid"]),None)
    if not key:raise ValueError("unknown identity signing key")
    return jwt.decode(token,jwt.PyJWK.from_dict(key).key,algorithms=["RS256"],
                      audience=audience,issuer=AUTH,options={"require":required})


def _bundle_key(passphrase,salt):
    passphrase="".join(passphrase.split()).replace("-","").upper()
    return base64.urlsafe_b64encode(hashlib.scrypt(passphrase.encode(),salt=salt,n=2**15,r=8,p=1,maxmem=64*1024*1024,dklen=32))


def seal_bundle(bundle):
    """Encrypt an export under a one-time passphrase that the admin is shown once: the file alone is useless."""
    code=base64.b32encode(secrets.token_bytes(15)).decode()  # 120 bits, read out in groups of four
    passphrase="-".join(code[i:i+4] for i in range(0,len(code),4));salt=os.urandom(16)
    token=Fernet(_bundle_key(passphrase,salt)).encrypt(json.dumps(bundle,separators=(",",":")).encode())
    return {"format":SEALED_FORMAT,"version":1,"salt":base64.b64encode(salt).decode(),"ciphertext":token.decode()},passphrase


def open_bundle(sealed,passphrase):
    try:
        if sealed.get("format")!=SEALED_FORMAT or sealed.get("version")!=1:raise ValueError("wrong format")
        if len(sealed["ciphertext"])>2*MAX_BUNDLE_BYTES:raise ValueError("oversized")
        salt=base64.b64decode(sealed["salt"],validate=True)
        return json.loads(Fernet(_bundle_key(passphrase,salt)).decrypt(sealed["ciphertext"].encode()))
    except (ValueError,KeyError,TypeError,AttributeError,InvalidToken):
        raise ValueError("The registration file or its passphrase is wrong. Export it again from the local installation.") from None

class ChatGPTAuth:
    def __init__(self,store):
        self.store=store; self.pending={}; self.lock=threading.RLock()
        if not store.get("host_id"):store.set("host_id","urn:uuid:"+str(uuid.uuid4()))

    def accounts(self):
        return self.store.get("chatgpt_accounts",[])

    def start(self,port,account_id=None):
        verifier=secrets.token_urlsafe(48);state=secrets.token_urlsafe(32);nonce=secrets.token_urlsafe(32)
        saved=self.store.secret("chatgpt:"+account_id) if account_id else None
        account=next((a for a in self.accounts() if a["id"]==account_id),None)
        if account_id and not account:raise ValueError("Unknown ChatGPT account")
        client=account["client_id"] if account else "dynamic_agent_client"
        redirect=f"http://127.0.0.1:{port}/auth/callback"
        query={"client_id":client,"ext_agent_host_id":self.store.get("host_id"),"response_type":"code",
               "redirect_uri":redirect,"scope":SCOPES,"resource":RESOURCE,"state":state,"nonce":nonce,
               "code_challenge_method":"S256","code_challenge":base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
        if not account:query["agent_name_hint"]="Inv Studio"
        elif saved and saved.get("id_token"):query["id_token_hint"]=saved["id_token"]
        with self.lock:
            self.pending={k:v for k,v in self.pending.items() if v["expires"]>time.time()}
            self.pending[state]={"verifier":verifier,"nonce":nonce,"redirect":redirect,"client":client,
                                 "account":account,"expires":time.time()+600}
        return AUTH+"/api/accounts/authorize?"+urlencode(query)

    def finish(self,params):
        with self.lock:pending=self.pending.pop(params.get("state",""),None)
        if not pending or pending["expires"]<time.time():raise ValueError("Sign-in expired or state was invalid. Start again.")
        if params.get("error"):raise ValueError("ChatGPT sign-in was declined or cancelled.")
        client=params.get("client_id") or pending["client"]
        if client=="dynamic_agent_client" or not params.get("code"):raise ValueError("Registration did not return an issued client and code.")
        if pending["account"] and client!=pending["client"]:raise ValueError("Registration changed during sign-in.")
        with httpx.Client(timeout=30,follow_redirects=False) as http:
            r=http.post(AUTH+"/api/accounts/oauth/token",data={"grant_type":"authorization_code","client_id":client,
                "code":params["code"],"code_verifier":pending["verifier"],"redirect_uri":pending["redirect"],"resource":RESOURCE})
            if r.status_code!=200:raise ValueError("Token exchange failed. Start a new sign-in.")
            tokens=r.json()
            keys=_identity_keys(http)
        claims=_verified_jwt(tokens["id_token"],keys,client,["exp","sub","nonce","aud","iss"])
        if not secrets.compare_digest(str(claims["nonce"]),pending["nonce"]):raise ValueError("Identity nonce did not match")
        if pending["account"] and claims["sub"]!=pending["account"]["subject"]:raise ValueError("Signed-in account changed")
        scopes=tokens.get("scope","").split()
        if "chatgpt.tokens.use.direct" not in scopes:raise ValueError("ChatGPT plan usage permission was not granted")
        aid=hashlib.sha256((client+claims["sub"]).encode()).hexdigest()[:20]
        tokens.update(client_id=client,expires_at=time.time()+int(tokens.get("expires_in",3600)),scopes=scopes)
        self.store.secret("chatgpt:"+aid,tokens)
        accounts=[a for a in self.accounts() if a["id"]!=aid]
        accounts.append({"id":aid,"email":claims.get("email","ChatGPT account"),"subject":claims["sub"],"client_id":client,"connected":True})
        self.store.set("chatgpt_accounts",accounts);self.store.set("chatgpt_active",aid)
        return aid

    def export_bundle(self,aid):
        """Return one explicit, Inv Studio-owned registration transfer bundle.

        The caller must serve this as a sensitive download. The target host ID is
        deliberately absent: importing must preserve the VM's existing host ID.
        """
        with self.lock:
            account=next((item for item in self.accounts() if item.get("id")==aid and item.get("connected")),None)
            if not account:raise ValueError("Connected ChatGPT account was not found")
            tokens=self._fresh_tokens(aid)
            try:
                credentials={key:_text(tokens[key]) for key in ("client_id","access_token","refresh_token")}
                credentials["expires_at"]=_timestamp(tokens["expires_at"])
                credentials["scopes"]=_scope_list(tokens["scopes"])
                _text(account["subject"],1,500);_display_text(account.get("email","ChatGPT account"),320)
            except Exception:
                raise ValueError("ChatGPT registration is incomplete. Reconnect it before exporting.") from None
            try:
                if "id_token" in tokens:credentials["id_token"]=_text(tokens["id_token"])
                if "token_type" in tokens:
                    if not isinstance(tokens["token_type"],str) or tokens["token_type"].lower()!="bearer":
                        raise ValueError("invalid token type")
                    credentials["token_type"]="Bearer"
                if "earliest_refresh_at" in tokens:
                    credentials["earliest_refresh_at"]=_timestamp(tokens["earliest_refresh_at"])
            except Exception:
                raise ValueError("ChatGPT registration is incomplete. Reconnect it before exporting.") from None
            bundle={"format":BUNDLE_FORMAT,"version":1,"exported_at":int(time.time()),
                    "account":{"subject":account["subject"],"email":account.get("email","ChatGPT account")},
                    "credentials":credentials}
            if len(json.dumps(bundle,separators=(",",":"),ensure_ascii=True).encode())>MAX_BUNDLE_BYTES:
                raise ValueError("ChatGPT credential bundle exceeds the size limit")
            # Transfer refresh ownership to the destination. Two hosts must not
            # race the same rotating refresh token after an explicit export.
            self.store.secret("chatgpt:"+aid,delete=True)
            accounts=self.accounts()
            for item in accounts:
                if item.get("id")==aid:item["connected"]=False
            self.store.set("chatgpt_accounts",accounts)
            if self.store.get("chatgpt_active")==aid:self.store.set("chatgpt_active",None)
            return bundle

    def import_bundle(self,bundle):
        """Verify and encrypt a registration created by :meth:`export_bundle`."""
        try:
            if len(json.dumps(bundle,separators=(",",":"),ensure_ascii=True).encode())>MAX_BUNDLE_BYTES:
                raise ValueError("oversized")
            root=_mapping(bundle,{"format","version","exported_at","account","credentials"},
                          {"format","version","exported_at","account","credentials"})
            if root["format"]!=BUNDLE_FORMAT or root["version"]!=1:
                raise ValueError("wrong format")
            exported_at=_timestamp(root["exported_at"])
            if exported_at>time.time()+300:raise ValueError("future export")
            account=_mapping(root["account"],{"subject","email"},{"subject","email"})
            subject=_text(account["subject"],1,500)
            _display_text(account["email"],320)
            allowed={"client_id","access_token","refresh_token","id_token","token_type",
                     "expires_at","earliest_refresh_at","scopes"}
            required={"client_id","access_token","refresh_token","expires_at","scopes"}
            credentials=_mapping(root["credentials"],required,allowed)
            client_id=_text(credentials["client_id"],3,500)
            access_token=_text(credentials["access_token"])
            refresh_token=_text(credentials["refresh_token"])
            declared_expiry=_timestamp(credentials["expires_at"])
            requested_scopes=_scope_list(credentials["scopes"])
            id_token=_text(credentials["id_token"]) if "id_token" in credentials else None
            token_type=credentials.get("token_type","Bearer")
            if not isinstance(token_type,str) or token_type.lower()!="bearer":raise ValueError("invalid token type")
            earliest=_timestamp(credentials["earliest_refresh_at"]) if "earliest_refresh_at" in credentials else None
            with httpx.Client(timeout=30,follow_redirects=False) as http:keys=_identity_keys(http)
            access_claims=_verified_jwt(access_token,keys,RESOURCE,
                                        ["exp","sub","aud","iss","client_id","scope"])
            if access_claims.get("client_id")!=client_id or access_claims.get("sub")!=subject:
                raise ValueError("registration does not match token")
            claim_scopes=access_claims.get("scope","").split()
            if "chatgpt.tokens.use.direct" not in claim_scopes or "offline_access" not in claim_scopes:
                raise ValueError("required scope missing")
            if set(requested_scopes)!=set(claim_scopes):raise ValueError("scope metadata mismatch")
            identity_claims=None
            if id_token:
                identity_claims=_verified_jwt(id_token,keys,client_id,["exp","sub","aud","iss"])
                if identity_claims.get("sub")!=subject:raise ValueError("identity subject mismatch")
            expires_at=float(access_claims["exp"])
            if expires_at<=time.time():raise ValueError("access token expired")
            if abs(declared_expiry-expires_at)>300:raise ValueError("expiry metadata mismatch")
            if earliest is not None and earliest>expires_at:raise ValueError("invalid refresh timing")
            verified_email=_display_text(identity_claims["email"],320) if identity_claims and identity_claims.get("email") else None
        except Exception:
            raise ValueError("ChatGPT credential bundle is invalid or expired. Sign in again locally and retry.") from None
        saved={"client_id":client_id,"access_token":access_token,"refresh_token":refresh_token,
               "expires_at":expires_at,"scopes":claim_scopes,"token_type":"Bearer"}
        if id_token:saved["id_token"]=id_token
        if earliest is not None:saved["earliest_refresh_at"]=earliest
        aid=hashlib.sha256((client_id+subject).encode()).hexdigest()[:20]
        with self.lock:
            # Never import or replace host_id. The VM keeps its own stable ID.
            self.store.secret("chatgpt:"+aid,saved)
            accounts=[item for item in self.accounts() if item.get("id")!=aid]
            accounts.append({"id":aid,"email":verified_email or "ChatGPT account","subject":subject,
                             "client_id":client_id,"connected":True})
            self.store.set("chatgpt_accounts",accounts);self.store.set("chatgpt_active",aid)
        return {"id":aid,"email":verified_email or "ChatGPT account","connected":True}

    def _fresh_tokens(self,aid):
        tokens=self.store.secret("chatgpt:"+aid) if aid else None
        if not tokens:raise ValueError("Connect a ChatGPT account first")
        if tokens["expires_at"]<time.time()+60:
            with httpx.Client(timeout=30) as http:
                response=http.post(AUTH+"/api/accounts/oauth/token",data={"grant_type":"refresh_token",
                    "client_id":tokens["client_id"],"refresh_token":tokens.get("refresh_token",""),"resource":RESOURCE})
            if response.status_code!=200:raise ValueError("ChatGPT session expired. Reconnect the selected account.")
            new=response.json();tokens.update(new);tokens["expires_at"]=time.time()+int(new.get("expires_in",3600))
            if "scope" in new:tokens["scopes"]=new["scope"].split()
            if "chatgpt.tokens.use.direct" not in tokens["scopes"]:
                raise ValueError("ChatGPT plan usage permission is no longer granted")
            self.store.secret("chatgpt:"+aid,tokens)
        return tokens

    def token(self):
        with self.lock:
            aid=self.store.get("chatgpt_active")
            return self._fresh_tokens(aid)["access_token"]

    def disconnect(self,aid):
        with self.lock:
            t=self.store.secret("chatgpt:"+aid);revoked=False
            if t:
                try:
                    with httpx.Client(timeout=20) as http:
                        cfg=http.get(AUTH+"/.well-known/openid-configuration").json()
                        endpoint=cfg["revocation_endpoint"]
                        if urlparse(endpoint).scheme=="https" and urlparse(endpoint).hostname=="auth.openai.com":
                            r=http.post(endpoint,data={"token":t.get("refresh_token",""),"token_type_hint":"refresh_token","client_id":t["client_id"]})
                            revoked=r.status_code==200
                except Exception:pass
            self.store.secret("chatgpt:"+aid,delete=True)
            accounts=self.accounts()
            for a in accounts:
                if a["id"]==aid:a["connected"]=False
            self.store.set("chatgpt_accounts",accounts)
            if self.store.get("chatgpt_active")==aid:self.store.set("chatgpt_active",None)
            return "Disconnected" if revoked else "Disconnected locally. Remote revocation was not confirmed; disconnect Inv Studio in ChatGPT Settings."
