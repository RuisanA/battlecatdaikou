import os
import asyncio
from dotenv import load_dotenv
import discord
from discord import app_commands, ui
from discord.ext import commands
import json
import time
import datetime
import hmac
import hashlib
import requests
import secrets
from typing import Any, Optional, Tuple
from bcsfe import cli, core
from bcsfe.cli import color
from bcsfe.core.game.catbase.gatya import GatyaEventType
from bcsfe.core.server.event_data import split_hhmm, split_yyyymmdd
from event_tickets import EventTickets

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

client = discord.Client(intents=discord.Intents.default())

CONFIG_FILE="config.json"

def load_config():
    try:
        with open(CONFIG_FILE,"r") as f:
            return json.load(f)
    except:
        return {}

def save_config(data):
    with open(CONFIG_FILE,"w") as f:
        json.dump(data,f,indent=4)

config=load_config()

class NyankoSignature:

    def __init__(self,inquiry_code:str,data:str):
        self.inquiry_code=inquiry_code
        self.data=data

    def generate_signature(self)->str:
        random_hex=secrets.token_hex(32)
        key=(self.inquiry_code+random_hex).encode()
        signature=hmac.new(key,self.data.encode(),hashlib.sha256).hexdigest()
        return random_hex+signature

    def generate_signature_v1(self)->str:
        data_double=self.data+self.data
        random_hex=secrets.token_hex(20)
        key=(self.inquiry_code+random_hex).encode()
        signature=hmac.new(key,data_double.encode(),hashlib.sha1).hexdigest()
        return random_hex+signature


class CloudEditor:
    AUTH_URL="https://nyanko-auth.ponosgames.com"
    SAVE_URL="https://nyanko-save.ponosgames.com"

    def __init__(self,transfer_code:str,pin:str,user,guild_id):
        self.transfer_code=transfer_code
        self.pin=pin
        self.session=requests.Session()
        self.save_file:Optional[Any]=None
        self.password=""
        self.last_error=""
        self.user=user
        self.guild_id=guild_id
        self.actions=[]

    def get_common_headers(self,iq:str,data:str)->dict:
        return{
        "Content-Type":"application/json",
        "Nyanko-Signature":NyankoSignature(iq,data).generate_signature(),
        "Nyanko-Timestamp":str(int(time.time())),
        "Nyanko-Signature-Version":"1",
        "Nyanko-Signature-Algorithm":"HMACSHA256",
        "User-Agent":"Dalvik/2.1.0"
        }

    def download_save(self)->bool:
        nonce=secrets.token_hex(16)
        url=f"{self.SAVE_URL}/v2/transfers/{self.transfer_code}/reception"
        payload={
        "clientInfo":{
        "client":{"version":"15.3.0","countryCode":"ja"},
        "os":{"type":"android","version":"13"},
        "device":{"model":"SM-S918B"}
        },
        "pin":self.pin,
        "nonce":nonce
        }
        body=json.dumps(payload,separators=(",",":"))
        headers={"Content-Type":"application/json"}
        try:
            res=self.session.post(url,headers=headers,data=body)
            if res.status_code==200 and res.headers.get("Content-Type")=="application/octet-stream":
                try:
                    from bcsfe.core.save_file import SaveFile as bSaveFile
                except:
                    bSaveFile=core.SaveFile
                self.save_file=bSaveFile(core.Data(res.content),cc=core.CountryCode("jp"))
                self.password=res.headers.get("Nyanko-Password","")
                return True
            self.last_error=res.text
        except Exception as e:
            self.last_error=str(e)
        return False

    def upload_save(self)->Tuple[Optional[str],Optional[str]]:

        if not self.save_file:
            return None,None
        
        if not hasattr(self.save_file, 'local_manager'):
            self.save_file.local_manager = None
            # 整合性チェック。エラーが出ても無視して次に進む
            try:
                self.save_file.patch()
            except:
                pass
        inq=self.save_file.inquiry_code
        try:
            login_data={
            "accountCode":inq,
            "password":self.password,
            "clientInfo":{
            "client":{"version":"15.3.0","countryCode":"ja"},
            "os":{"type":"android","version":"9"},
            "device":{"model":"SM-G955F"}
            },
            "nonce":secrets.token_hex(16)
            }
            login_body=json.dumps(login_data,separators=(",",":"))
            h1=self.get_common_headers(inq,login_body)
            res1=self.session.post(f"{self.AUTH_URL}/v1/tokens",headers=h1,data=login_body)
            if res1.status_code!=200:
                self.last_error=res1.text
                return None,None
            token=res1.json()["payload"]["token"]
            nonce_aws=secrets.token_hex(16)
            h2=self.get_common_headers(inq,"")
            h2["Authorization"]=f"Bearer {token}"
            res2=self.session.get(f"{self.SAVE_URL}/v2/save/key?nonce={nonce_aws}",headers=h2)
            aws=res2.json()["payload"]
            modified_bytes=self.save_file.to_data().to_bytes()
            files={"file":("file.sav",modified_bytes,"application/octet-stream")}
            s3_data={k:v for k,v in aws.items() if k!="url"}
            requests.post(aws["url"],data=s3_data,files=files)
            meta_payload={
            "managedItemDetails":[],
            "nonce":secrets.token_hex(16),
            "playTime":self.save_file.officer_pass.play_time,
            "rank":self.save_file.calculate_user_rank(),
            "receiptLogIds":[],
            "saveKey":aws["key"],
            "signature_v1":NyankoSignature(inq,"[]").generate_signature_v1()
            }
            meta_body=json.dumps(meta_payload,separators=(",",":"))
            h4=self.get_common_headers(inq,meta_body)
            h4["Authorization"]=f"Bearer {token}"
            res4=self.session.post(f"{self.SAVE_URL}/v2/transfers",headers=h4,data=meta_body)
            p=res4.json()["payload"]
            return p.get("transferCode"),p.get("pin")
        except Exception as e:
            self.last_error=str(e)
            return None,None

class MultiValueModal(ui.Modal):
    def __init__(self,editor,values):
        super().__init__(title="数値入力")
        self.editor=editor
        self.values=values
        self.inputs={}
        if "catfood" in values:
            t=ui.TextInput(label="ネコカン")
            self.inputs["catfood"]=t
            self.add_item(t)
        if "xp" in values:
            t=ui.TextInput(label="XP")
            self.inputs["xp"]=t
            self.add_item(t)
        if "rare" in values:
            t=ui.TextInput(label="レアチケット")
            self.inputs["rare"]=t
            self.add_item(t)
        if "normal" in values:
            t = ui.TextInput(label="にゃんこチケット")
            self.inputs["normal"] = t
            self.add_item(t)
        if "platinum" in values:
            t = ui.TextInput(label="プラチナチケット")
            self.inputs["platinum"] = t
            self.add_item(t)
        if "legend" in values:
            t = ui.TextInput(label="レジェンドチケット")
            self.inputs["legend"] = t
            self.add_item(t)
        if "np" in values:
            t=ui.TextInput(label="NP")
            self.inputs["np"]=t
            self.add_item(t)
        if "lead" in values:
            t=ui.TextInput(label="リーダーシップ")
            self.inputs["lead"]=t
            self.add_item(t)
        if "battleitem" in values:
            t = ui.TextInput(label="戦闘アイテム")
            self.inputs["battleitem"] = t
            self.add_item(t)
        if "unlock_cats" in values:
            t = ui.TextInput(label="全キャラ解放")
            self.inputs["unlock_cats"] = t
            self.add_item(t)
        if "remove_error_cats" in values:
            t = ui.TextInput(label="エラーキャラ削除")
            self.inputs["remove_error_cats"] = t
            self.add_item(t)
        if "unlock_stages" in values:
            t = ui.TextInput(label="全ステージ解放")
            self.inputs["unlock_stages"] = t
            self.add_item(t)
        if "catseye" in values:
            t = ui.TextInput(label="キャッツアイ")
            self.inputs["catseye"] = t
            self.add_item(t)
        if "event_ticket" in values:
            t = ui.TextInput(label="イベントチケット", default="999")
            self.inputs["event_ticket"] = t
            self.add_item(t)

    async def on_submit(self,interaction:discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        s=self.editor.save_file
        actions=[]
        for k,v in self.inputs.items():
            if v.value=="":
                continue
            num=int(v.value)
            if k=="catfood":
                s.set_catfood(num)
                actions.append(f"ネコカン {num}")
            elif k=="xp":
                s.set_xp(num)
                actions.append(f"XP {num}")
            elif k=="rare":
                s.set_rare_tickets(num)
                actions.append(f"レアチケット {num}")
            elif k=="normal":
                s.set_normal_tickets(num)
                actions.append(f"にゃんこチケット {num}")
            elif k=="platinum":
                s.set_platinum_tickets(num)
                actions.append(f"プラチナチケット {num}")
            elif k=="legend":
                s.set_legend_tickets(num)
                actions.append(f"レジェンドチケット {num}")
            elif k=="np":
                s.set_np(num)
                actions.append(f"NP {num}")
            elif k=="lead":
                s.set_leadership(num)
                actions.append(f"リーダーシップ {num}")
            elif k == "battleitem":  # あなたが設定した名前に合わせました
                try:
                    # s.battle_items.items を全ループして数値をセット
                    for i in range(len(s.battle_items.items)):
                        s.battle_items.items[i] = num
                    actions.append(f"バトルアイテム全種 {num}")
                except Exception as e:
                    print(f"Battle Item Error: {e}")
            elif "unlock_cats" in self.values:
                for cat in s.cats.cats:
                    # 1. 解放フラグ
                    cat.unlocked = True
                    
                    # 2. 所持状態にする
                    if hasattr(cat, 'set_obtained'):
                        cat.set_obtained(True)
                    
                    # 3. レベル設定 (Upgradeオブジェクト内の変数名を柔軟に探す)
                    if hasattr(cat, 'upgrade') and cat.upgrade is not None:
                        # 内部では base_lv という名前が使われていることが多いです
                        if hasattr(cat.upgrade, 'base_lv'):
                            if cat.upgrade.base_lv < 0:
                                cat.upgrade.base_lv = 0
                        elif hasattr(cat.upgrade, 'level'):
                            if cat.upgrade.level < 0:
                                cat.upgrade.level = 0
                                
                actions.append("全キャラ解放")
            elif k == "remove_error_cats":
                # 1. 存在しないIDや異常なデータを特定して削除
                # s.cats.cats は全キャラのリスト
                original_count = len(s.cats.cats)
                
                # 正常なキャラだけを残すフィルタリング例
                # IDが負、または極端に大きいものを除外する場合
                s.cats.cats = [cat for cat in s.cats.cats if 0 <= cat.id < 1000]
                
                # 2. あるいは、特定の「エラーキャラ」フラグを持つものをリセット
                for cat in s.cats.cats:
                    # 名前が取得できない、あるいはデータが空のキャラを未所持に戻す
                    if not hasattr(cat, 'upgrade') or cat.upgrade is None:
                        cat.unlocked = False
                        if hasattr(cat, 'set_obtained'):
                            cat.set_obtained(False)
                
                actions.append("エラーキャラ削除・リセット完了")

            elif k == "unlock_stages":
             core.StoryChapters.clear_tutorial(self.editor.save_file)
             story_chapters = self.editor.save_file.story.get_real_chapters()
             for chapter in story_chapters:
                 chapter.clear_chapter()
                 for stage in chapter.get_valid_treasure_stages():
                     stage.set_treasure(3)
                     print("全ステージ解放・お宝コンプ完了")
                     actions.append("全ステージ解放・お宝コンプ完了")
            
            elif k == "catseye":
                raw_val = self.inputs["catseye"].value
                amount = int(raw_val) if raw_val.isdigit() else 999
                num_categories = len(self.editor.save_file.catseyes)
                self.editor.save_file.catseyes = [amount] * num_categories
                print(f"全種類のキャッツアイを {amount} 個に設定しました")

            elif k == "event_ticket":
                try:
                    
                    user_input = self.inputs["event_ticket"].value
                    amount = int(user_input) if user_input.isdigit() else 999
                except Exception:
                    
                    amount = 999

            # 1. サーバーから生のイベントデータを直接取得
            try:
                handler = core.ServerHandler(self.editor.save_file)
                gatya_data_raw = handler.download_gatya_data()
                
                if gatya_data_raw is None:
                    print("Log: イベントデータのダウンロードに失敗しました。")
                    continue
                
                # ServerGatyaDataをパース
                gatya_data = core.ServerGatyaData.from_data(gatya_data_raw)
            except Exception as e:
                print(f"Log: イベントデータ取得中にエラー: {e}")
                continue

            # 2. 現在のセーブデータ内のリストを直接書き換え
            # ガチャデータから「イベントチケット」に関連するIDを探し、枚数を設定する
            updated = False
            for item in gatya_data.items:
                # 開催中の全ガチャセットを確認
                for gset in item.sets:
                    if gset.number == -1: continue
                    
                    # チケットIDを取得 (bcsfeの内部ID体系を使用)
                    # ここでは安全に、セーブデータの全チケット枠をamountに設定する「一括モード」を適用します
                    updated = True

            # 3. セーブデータの各チケット配列を直接一括更新
            # 多くのイベントチケットは以下の3つのリストに格納されています
            try:
                # 福引ガチャチケットなど
                self.editor.save_file.lucky_tickets = [amount] * len(self.editor.save_file.lucky_tickets)
                # イベントガチャチケット1
                self.editor.save_file.event_capsules = [amount] * len(self.editor.save_file.event_capsules)
                # イベントガチャチケット2
                self.editor.save_file.event_capsules_2 = [amount] * len(self.editor.save_file.event_capsules_2)
                
                print(f"Log: すべてのイベントチケット枠を {amount} 枚に設定しました。")
            except Exception as e:
                print(f"Log: チケット書き換え中にエラー: {e}")

        t_code,pin=self.editor.upload_save()
        if t_code:
             dm=discord.Embed(title="代行完了",color=0x2ecc71)
             dm.add_field(name="引継ぎコード",value=f"`{t_code}`",inline=False)
             dm.add_field(name="認証コード",value=f"`{pin}`",inline=False)
             dm.set_footer(text="必ず保存してください")
        try:
                await interaction.user.send(embed=dm)
        except:
                pass
        done=discord.Embed(title="代行完了",description="DMに引継ぎコードを送信しました",color=0x2ecc71)
        await interaction.followup.send(embed=done,ephemeral=True)
        gid=str(self.editor.guild_id)
        if gid in config:
                ch=interaction.client.get_channel(config[gid])
                if ch:
                    log=discord.Embed(title="にゃんこ大戦争代行ログ",color=0x3498db)
                    log.set_author(name=self.editor.user.name,icon_url=self.editor.user.display_avatar.url)
                    log.add_field(name="購入者",value=self.editor.user.mention)
                    log.add_field(name="内容",value="\n".join(actions))
                    log.add_field(name="日時",value=f"<t:{int(time.time())}:F>")
                    await ch.send(embed=log)
                else:err=discord.Embed(title="エラー",description=f"```{self.editor.last_error}```",color=0xff0000)
        await interaction.followup.send(embed=err,ephemeral=True)

class ModDropdown(ui.Select):
    def __init__(self,editor):
        self.editor=editor
        options=[
        discord.SelectOption(label="1,猫缶",value="catfood"),
        discord.SelectOption(label="2,XP",value="xp"),
        discord.SelectOption(label="3,レアチケット",value="rare"),
        discord.SelectOption(label="4,にゃんこチケット", value="normal"),
        discord.SelectOption(label="5,プラチナチケット", value="platinum"),
        discord.SelectOption(label="6,レジェンドチケット", value="legend"),
        discord.SelectOption(label="7,NP",value="np"),
        discord.SelectOption(label="8,リーダーシップ",value="lead"),
        discord.SelectOption(label="9,戦闘アイテム", value="battleitem"),
        discord.SelectOption(label="10,全キャラ解放", value="unlock_cats"),
        discord.SelectOption(label="11,エラーキャラ削除", value="remove_error_cats"),
        discord.SelectOption(label="12,全ステージ解放", value="unlock_stages"),
        discord.SelectOption(label="13,キャッツアイ", value="catseye"),
        discord.SelectOption(label="14,イベントチケット", value="event_ticket"),
        ]
        super().__init__(placeholder="適用する項目をすべて選んでください...",min_values=1,max_values=len(options),options=options)
    async def callback(self,interaction:discord.Interaction):
        await interaction.response.send_modal(MultiValueModal(self.editor,self.values))


class LoginModal(ui.Modal, title="代行ログイン"):
    t = ui.TextInput(label="引き継ぎコード")
    p = ui.TextInput(label="認証コード", min_length=4, max_length=4)

    async def on_submit(self, interaction: discord.Interaction):
        # 1. ログイン開始のログ
        print(f"--- ログイン処理開始 ---")
        print(f"ユーザー: {interaction.user} (ID: {interaction.user.id})")
        print(f"引き継ぎコード: {self.t.value}")
        print(f"認証コード: {self.p.value}")
        print("ステータス: ログイン中...")

        await interaction.response.defer(ephemeral=True)

        # CloudEditorの初期化
        editor = CloudEditor(self.t.value, self.p.value, interaction.user, interaction.guild.id)

        # 2. ダウンロード処理の実行と結果ログ
        if editor.download_save():
            print("ステータス: ログイン完了")
            print(f"------------------------")

            embed = discord.Embed(title="ログイン完了", description="適用する項目を選択してください", color=0x5865F2)
            view = ui.View()
            view.add_item(ModDropdown(editor))
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            # 3. 失敗時のログ
            print(f"ステータス: ログイン失敗")
            print(f"エラー内容: {editor.last_error}")
            print(f"------------------------")

            err = discord.Embed(title="ログインエラー", description=f"```{editor.last_error}```", color=0xff0000)
            await interaction.followup.send(embed=err, ephemeral=True)


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!",intents=discord.Intents.all())
    async def setup_hook(self):
        await self.tree.sync()

bot=MyBot()

class PersistentLoginView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(
        label="ログイン", 
        style=discord.ButtonStyle.success, 
        custom_id="persistent_bc_login" # これが再起動後の識別に必須です
    )
    async def login_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 前に作成した LoginModal を呼び出す
        await interaction.response.send_modal(LoginModal())

@bot.tree.command(name="チャンネル設定")
@app_commands.checks.has_permissions(administrator=True)
async def channel_set(interaction:discord.Interaction,channel:discord.TextChannel):
    config[str(interaction.guild.id)]=channel.id
    save_config(config)
    embed=discord.Embed(title="ログチャンネル設定",description= "設定しました",color=0x2ecc71)
    await interaction.response.send_message(embed=embed,ephemeral=True)

@bot.tree.command(name="にゃんこ大戦争代行")
@app_commands.checks.has_permissions(administrator=True)
async def battlecats(interaction:discord.Interaction):
    embed=discord.Embed(title="にゃんこ大戦争自動代行",description="引き継ぎコードと認証コードに間違いがないようにしてください\n\n1,猫缶 150円\n2,XP 400円\n3,レアチケットカンスト 400円\n4,にゃんこチケットカンスト 200円\n5,プラチナチケット 500円\n6,レジェンドチケット  500円\n7,NP 300円\n8,リーダーシップ 500円\n9,戦闘アイテム 400円\n10,全キャラ解放 400円\n11,エラーキャラ削除 200円\n12,全ステージ解放 200円\n13,キャッツアイ 500円\n14,イベントチケット 500円\n\nお支払い方法 PayPay",color=0x2b2d31)
    view=ui.View()
    btn=ui.Button(label="ログイン",style=discord.ButtonStyle.success)
    async def login_cb(it):
        await it.response.send_modal(LoginModal())
    btn.callback=login_cb
    view.add_item(btn)
    await interaction.response.send_message(embed=embed, view=PersistentLoginView())

CONST_VALUES = {
    "catfood": 58999,
    "xp": 999999999,
    "rare": 999,
    "normal": 999,
    "platinum": 999,
    "legend": 999,
    "np": 999999999,
    "lead": 999,
    "battleitem": 999,
    "catseye": 999,
    "event_ticket": 999
}

LABEL_MAP = {
    "catfood": "1,猫缶",
    "xp": "2,XP",
    "rare": "3,レアチケットカンスト",
    "normal": "4,にゃんこチケットカンスト",
    "platinum": "5,プラチナチケット",
    "legend": "6,レジェンドチケット",
    "np": "7,NP",
    "lead": "8,リーダーシップ",
    "battleitem": "9,戦闘アイテム",
    "unlock_cats": "10,全キャラ解放",
    "remove_error_cats": "11,エラーキャラ削除",
    "unlock_stages": "12,全ステージ解放",
    "catseye": "13,キャッツアイ",
    "event_ticket": "14,イベントチケット"
}

LABEL_MAP2 = {
    "sub15": "15,指定キャラ第3形態1体",
    "sub16": "16,ステージ進行 1編",
    "sub17": "17,マタタビ全種類カンスト",
    "sub18": "18,BAN保証",
    "sub19": "19,永久BAN保証",
    "sub20": "20,永久猫缶補充"
}

def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=4)

config = load_config()

class NyankoSignature:
    def __init__(self, inquiry_code: str, data: str):
        self.inquiry_code = inquiry_code
        self.data = data

    def generate_signature(self) -> str:
        random_hex = secrets.token_hex(32)
        key = (self.inquiry_code + random_hex).encode()
        signature = hmac.new(key, self.data.encode(), hashlib.sha256).hexdigest()
        return random_hex + signature

    def generate_signature_v1(self) -> str:
        data_double = self.data + self.data
        random_hex = secrets.token_hex(20)
        key = (self.inquiry_code + random_hex).encode()
        signature = hmac.new(key, data_double.encode(), hashlib.sha1).hexdigest()
        return random_hex + signature

class CloudEditor:
    AUTH_URL = "https://nyanko-auth.ponosgames.com"
    SAVE_URL = "https://nyanko-save.ponosgames.com"

    def __init__(self, transfer_code: str, pin: str, user, guild_id):
        self.transfer_code = transfer_code
        self.pin = pin
        self.session = requests.Session()
        self.save_file: Optional[Any] = None
        self.password = ""
        self.last_error = ""
        self.user = user
        self.guild_id = guild_id

    def get_common_headers(self, iq: str, data: str) -> dict:
        return {
            "Content-Type": "application/json",
            "Nyanko-Signature": NyankoSignature(iq, data).generate_signature(),
            "Nyanko-Timestamp": str(int(time.time())),
            "Nyanko-Signature-Version": "1",
            "Nyanko-Signature-Algorithm": "HMACSHA256",
            "User-Agent": "Dalvik/2.1.0"
        }

    def download_save(self) -> bool:
        nonce = secrets.token_hex(16)
        url = f"{self.SAVE_URL}/v2/transfers/{self.transfer_code}/reception"
        payload = {
            "clientInfo": {
                "client": {"version": "15.3.0", "countryCode": "ja"},
                "os": {"type": "android", "version": "13"},
                "device": {"model": "SM-S918B"}
            },
            "pin": self.pin,
            "nonce": nonce
        }
        body = json.dumps(payload, separators=(",", ":"))
        headers = {"Content-Type": "application/json"}
        try:
            res = self.session.post(url, headers=headers, data=body)
            if res.status_code == 200 and res.headers.get("Content-Type") == "application/octet-stream":
                try:
                    from bcsfe.core.save_file import SaveFile as bSaveFile
                except:
                    bSaveFile = core.SaveFile
                self.save_file = bSaveFile(core.Data(res.content), cc=core.CountryCode("jp"))
                self.password = res.headers.get("Nyanko-Password", "")
                return True
            self.last_error = res.text
        except Exception as e:
            self.last_error = str(e)
        return False

    def upload_save(self) -> Tuple[Optional[str], Optional[str]]:
        if not self.save_file:
            return None, None
        
        if not hasattr(self.save_file, 'local_manager'):
            self.save_file.local_manager = None
            try:
                self.save_file.patch()
            except:
                pass
        inq = self.save_file.inquiry_code
        try:
            login_data = {
                "accountCode": inq,
                "password": self.password,
                "clientInfo": {
                    "client": {"version": "15.3.0", "countryCode": "ja"},
                    "os": {"type": "android", "version": "9"},
                    "device": {"model": "SM-G955F"}
                },
                "nonce": secrets.token_hex(16)
            }
            login_body = json.dumps(login_data, separators=(",", ":"))
            h1 = self.get_common_headers(inq, login_body)
            res1 = self.session.post(f"{self.AUTH_URL}/v1/tokens", headers=h1, data=login_body)
            if res1.status_code != 200:
                self.last_error = res1.text
                return None, None
            token = res1.json()["payload"]["token"]
            nonce_aws = secrets.token_hex(16)
            h2 = self.get_common_headers(inq, "")
            h2["Authorization"] = f"Bearer {token}"
            res2 = self.session.get(f"{self.SAVE_URL}/v2/save/key?nonce={nonce_aws}", headers=h2)
            aws = res2.json()["payload"]
            modified_bytes = self.save_file.to_data().to_bytes()
            files = {"file": ("file.sav", modified_bytes, "application/octet-stream")}
            s3_data = {k: v for k, v in aws.items() if k != "url"}
            requests.post(aws["url"], data=s3_data, files=files)
            meta_payload = {
                "managedItemDetails": [],
                "nonce": secrets.token_hex(16),
                "playTime": self.save_file.officer_pass.play_time,
                "rank": self.save_file.calculate_user_rank(),
                "receiptLogIds": [],
                "saveKey": aws["key"],
                "signature_v1": NyankoSignature(inq, "[]").generate_signature_v1()
            }
            meta_body = json.dumps(meta_payload, separators=(",", ":"))
            h4 = self.get_common_headers(inq, meta_body)
            h4["Authorization"] = f"Bearer {token}"
            res4 = self.session.post(f"{self.SAVE_URL}/v2/transfers", headers=h4, data=meta_body)
            p = res4.json()["payload"]
            return p.get("transferCode"), p.get("pin")
        except Exception as e:
            self.last_error = str(e)
            return None, None

class TicketView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @ui.button(label="送金処理:未", style=discord.ButtonStyle.success, custom_id="persistent_ticket_start")
    async def start_button(self, interaction: discord.Interaction, button: ui.Button):
        # 権限チェック（設定されたスタッフロールを持っているか）
        guild_id = str(interaction.guild.id)
        role_id = config.get(guild_id, {}).get("role")
        
        if role_id:
            staff_role = interaction.guild.get_role(role_id)
            if staff_role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message("この操作を行う権限がありません。", ephemeral=True)
                return

        await interaction.response.defer()
        
        embed = interaction.message.embeds[0]
        REVERSE_MAP = {v: k for k, v in LABEL_MAP.items()}
        selected_values = [REVERSE_MAP[label.strip()] for label in embed.fields[0].value.split("\n")]
        tc = embed.fields[2].value
        pin = embed.fields[3].value
        user_id_str = embed.footer.text.replace("ユーザーID: ", "")
        
        try:
            target_user = await interaction.client.fetch_user(int(user_id_str))
        except:
            target_user = None

        editor = CloudEditor(tc, pin, target_user, interaction.guild.id)
        if not editor.download_save():
            await interaction.followup.send(f"ログインエラー:\n```{editor.last_error}```")
            return

        s = editor.save_file
        actions = []

        # 選択された内容に基づいて自動処理
        for val in selected_values:
            val = val.strip()
            num = CONST_VALUES.get(val, 0)

            if val == "catfood":
                s.set_catfood(num)
                actions.append(f"猫缶 {num}")
            elif val == "xp":
                s.set_xp(num)
                actions.append(f"XP {num}")
            elif val == "rare":
                s.set_rare_tickets(num)
                actions.append(f"レアチケット {num}")
            elif val == "normal":
                s.set_normal_tickets(num)
                actions.append(f"にゃんこチケット {num}")
            elif val == "platinum":
                s.set_platinum_tickets(num)
                actions.append(f"プラチナチケット {num}")
            elif val == "legend":
                s.set_legend_tickets(num)
                actions.append(f"レジェンドチケット {num}")
            elif val == "np":
                s.set_np(num)
                actions.append(f"NP {num}")
            elif val == "lead":
                s.set_leadership(num)
                actions.append(f"リーダーシップ {num}")
            elif val == "battleitem":
                try:
                    for i in range(len(s.battle_items.items)):
                        s.battle_items.items[i] = num
                    actions.append(f"バトルアイテム全種 {num}")
                except Exception as e: print(f"Battle Item Error: {e}")
            elif val == "unlock_cats":
                for cat in s.cats.cats:
                    cat.unlocked = True
                    if hasattr(cat, 'set_obtained'): cat.set_obtained(True)
                    if hasattr(cat, 'upgrade') and cat.upgrade is not None:
                        if hasattr(cat.upgrade, 'base_lv') and cat.upgrade.base_lv < 0:
                            cat.upgrade.base_lv = 0
                        elif hasattr(cat.upgrade, 'level') and cat.upgrade.level < 0:
                            cat.upgrade.level = 0
                actions.append("全キャラ解放")
            elif val == "remove_error_cats":
                s.cats.cats = [cat for cat in s.cats.cats if 0 <= cat.id < 1000]
                for cat in s.cats.cats:
                    if not hasattr(cat, 'upgrade') or cat.upgrade is None:
                        cat.unlocked = False
                        if hasattr(cat, 'set_obtained'): cat.set_obtained(False)
                actions.append("エラーキャラ削除・リセット完了")
            elif val == "unlock_stages":
                core.StoryChapters.clear_tutorial(s)
                story_chapters = s.story.get_real_chapters()
                for chapter in story_chapters:
                    chapter.clear_chapter()
                    for stage in chapter.get_valid_treasure_stages():
                        stage.set_treasure(3)
                actions.append("全ステージ解放・お宝コンプ")
            elif val == "catseye":
                s.catseyes = [num] * len(s.catseyes)
                actions.append(f"キャッツアイ全種 {num}")
            elif val == "event_ticket":
                try:
                    s.lucky_tickets = [num] * len(s.lucky_tickets)
                    s.event_capsules = [num] * len(s.event_capsules)
                    s.event_capsules_2 = [num] * len(s.event_capsules_2)
                    actions.append(f"イベントチケット各種 {num}")
                except: pass

        t_code, pin_code = editor.upload_save()
        if t_code:
            if target_user:
                dm = discord.Embed(title="代行完了", color=0x2ecc71)
                dm.add_field(name="引継ぎコード", value=f"`{t_code}`", inline=False)
                dm.add_field(name="認証コード", value=f"`{pin_code}`", inline=False)
                dm.add_field(name="ご依頼内容", value="\n".join(actions), inline=False)
                dm.set_footer(text="必ず保存してください")
                try:
                    await target_user.send(embed=dm)
                except: pass
            
            # ボタンを無効化
            for item in self.children:
                if item.custom_id == "persistent_ticket_start":
                    item.disabled = True
                    item.label = "送金処理:済"
            await interaction.message.edit(view=self)

            finish_embed = discord.Embed(
                title="代行完了",
                description="代行が完了しました DMにログイン情報が送信されています\n確認次第 <#1399314060373135400> に実績記入をお願い致します\n\n以下のような場合はお問い合わせください\n・ご依頼内容が反映されていない\n・ログイン情報が届かない,ログインできない\n・その他の不具合",
                color=discord.Color.random(),
                timestamp=datetime.datetime.now(datetime.timezone.utc))
            
            await interaction.channel.send(embed=finish_embed)
        else:
            error_embed = discord.Embed(
                title="エラー",
                description="処理中にエラーが発生しました",
                color=discord.Color.random(),
                timestamp=datetime.datetime.now(datetime.timezone.utc))
            await interaction.channel.send(embed=error_embed)
            print(editor.last_error)

    @ui.button(label="チケットを削除", style=discord.ButtonStyle.danger, custom_id="persistent_ticket_delete")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = str(interaction.guild.id)
        role_id = config.get(guild_id, {}).get("role")
        if role_id:
            staff_role = interaction.guild.get_role(role_id)
            if staff_role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message("この操作を行う権限がありません。", ephemeral=True)
                return

        embed = discord.Embed(
            title="チケットを閉じる",
            description="本当にチケットを閉じますか？",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, view=DeleteConfirmView(), ephemeral=True)

class TicketView2(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="チケットを削除", style=discord.ButtonStyle.danger, custom_id="persistent_ticket_delete2")
    async def delete_button2(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = str(interaction.guild.id)
        role_id = config.get(guild_id, {}).get("role")
        if role_id:
            staff_role = interaction.guild.get_role(role_id)
            if staff_role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message("この操作を行う権限がありません。", ephemeral=True)
                return
        
        embed = discord.Embed(
            title="チケットを閉じる",
            description="本当にチケットを閉じますか？",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, view=DeleteConfirmView(), ephemeral=True)

class DeleteConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60) # 60秒でタイムアウト

    @discord.ui.button(label="閉じる", style=discord.ButtonStyle.success, custom_id="confirm_delete")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 削除実行
        embed = discord.Embed(
            title="チケットを閉じる",
            description="チケットを閉じます",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
        await asyncio.sleep(3)
        await interaction.channel.delete()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.danger, custom_id="cancel_delete")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # キャンセル処理
        embed = discord.Embed(
            title="キャンセル",
            description="チケットの削除をキャンセルしました。",
            color=discord.Color.greyple()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        # 確認メッセージを削除（または非表示）にしたい場合はここで処理

class PurchaseModal(ui.Modal, title="購入情報入力フォーム"):
    paypay = ui.TextInput(label="送金リンク", style=discord.TextStyle.long, placeholder="PayPayのリンクを入力", required=True)
    tc = ui.TextInput(label="引き継ぎコード", placeholder="abcdef12345", required=True)
    pin = ui.TextInput(label="認証コード", placeholder="1234", min_length=4, max_length=4, required=True)

    def __init__(self, selected_values: str):
        super().__init__()
        self.selected_values = selected_values

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # 1. 入力されたコードでログイン検証
        editor = CloudEditor(self.tc.value, self.pin.value, interaction.user, interaction.guild.id)
        if not editor.download_save():
            err_embed = discord.Embed(title="ログインエラー", description=f"```{editor.last_error}```", color=discord.Color.red())
            await interaction.followup.send(embed=err_embed, ephemeral=True)
            return

        # 2. チケットチャンネルの作成
        guild = interaction.guild
        guild_id = str(guild.id)
        
        category_id = config.get(guild_id, {}).get("category")
        role_id = config.get(guild_id, {}).get("role")
        
        category = guild.get_channel(category_id) if category_id else None
        
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }
        
        staff_role = None
        if role_id:
            staff_role = guild.get_role(role_id)
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        new_channel = await guild.create_text_channel(
            name=f"🎫｜{interaction.user.name}",
            category=category,
            overwrites=overwrites,
            topic=str(interaction.user.id)
        )

        info_embed = discord.Embed(title="送金処理完了までお待ちください", color=discord.Color.blue())
        display_texts = [LABEL_MAP[v.strip()] for v in self.selected_values.split(",")]
        display_value = "\n".join(display_texts)
        info_embed.add_field(name="依頼内容", value=display_value, inline=False)
        info_embed.add_field(name="送金リンク", value=self.paypay.value, inline=False)
        info_embed.add_field(name="引き継ぎコード", value=self.tc.value, inline=False)
        info_embed.add_field(name="認証コード", value=self.pin.value, inline=False)
        info_embed.set_footer(text=f"ユーザーID: {interaction.user.id}")

        await new_channel.send(f"{interaction.user.mention}", embed=info_embed, view=TicketView())

        if staff_role:
            msg = await new_channel.send(staff_role.mention)
            await asyncio.sleep(3)
            await msg.delete()

        await interaction.followup.send(f"ログイン成功 チケットが発行されました: {new_channel.mention}", ephemeral=True)

class PurchaseModal2(ui.Modal, title="購入情報入力フォーム"):
    paypay = ui.TextInput(label="送金リンク", style=discord.TextStyle.long, placeholder="PayPayのリンクを入力", required=True)
    tc = ui.TextInput(label="引き継ぎコード", placeholder="abcdef12345", required=True)
    pin = ui.TextInput(label="認証コード", placeholder="1234", min_length=4, max_length=4, required=True)

    def __init__(self, selected_values: str):
        super().__init__()
        self.selected_values = selected_values

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # 2. チケットチャンネルの作成
        guild = interaction.guild
        guild_id = str(guild.id)
        
        category_id = config.get(guild_id, {}).get("category")
        role_id = config.get(guild_id, {}).get("role")
        
        category = guild.get_channel(category_id) if category_id else None
        
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }
        
        staff_role = None
        if role_id:
            staff_role = guild.get_role(role_id)
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        new_channel = await guild.create_text_channel(
            name=f"🎫｜{interaction.user.name}",
            category=category,
            overwrites=overwrites,
            topic=str(interaction.user.id)
        )

        info_embed = discord.Embed(title="送金処理完了までお待ちください", color=discord.Color.blue())
        display_texts = [LABEL_MAP2[v.strip()] for v in self.selected_values.split(",")]
        display_value = "\n".join(display_texts)
        info_embed.add_field(name="依頼内容", value=display_value, inline=False)
        info_embed.add_field(name="送金リンク", value=self.paypay.value, inline=False)
        info_embed.add_field(name="引き継ぎコード", value=self.tc.value, inline=False)
        info_embed.add_field(name="認証コード", value=self.pin.value, inline=False)
        info_embed.set_footer(text=f"ユーザーID: {interaction.user.id}")

        await new_channel.send(f"{interaction.user.mention}", embed=info_embed, view=TicketView2())

        if staff_role:
            msg = await new_channel.send(staff_role.mention)
            await asyncio.sleep(3)
            await msg.delete()

        await interaction.followup.send(f"チケットが発行されました: {new_channel.mention}", ephemeral=True)

class OrderSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="1,猫缶 58999", value="catfood"),
            discord.SelectOption(label="2,XP 999999999", value="xp"),
            discord.SelectOption(label="3,レアチケット 999", value="rare"),
            discord.SelectOption(label="4,にゃんこチケット 999", value="normal"),
            discord.SelectOption(label="5,プラチナチケット 999", value="platinum"),
            discord.SelectOption(label="6,レジェンドチケット 999", value="legend"),
            discord.SelectOption(label="7,NP 999999999", value="np"),
            discord.SelectOption(label="8,リーダーシップ", value="lead"),
            discord.SelectOption(label="9,戦闘アイテム", value="battleitem"),
            discord.SelectOption(label="10,全キャラ解放", value="unlock_cats"),
            discord.SelectOption(label="11,エラーキャラ削除", value="remove_error_cats"),
            discord.SelectOption(label="12,全ステージ解放", value="unlock_stages"),
            discord.SelectOption(label="13,キャッツアイ", value="catseye"),
            discord.SelectOption(label="14,イベントチケット", value="event_ticket")
        ]
        super().__init__(placeholder="適用する項目をすべて選んでください...", min_values=1, max_values=len(options), options=options)

    async def callback(self, interaction: discord.Interaction):
        # 選択された依頼内容をカンマ区切りの文字列にして Modal に渡す
        selected_values = ",".join(self.values)
        await interaction.response.send_modal(PurchaseModal(selected_values))

class OrderSelect2(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="15,指定キャラ第3形態1体につき", value="sub15"),
            discord.SelectOption(label="16,ステージ進行 1編につき", value="sub16"),
            discord.SelectOption(label="17,マタタビ全種類カンスト", value="sub17"),
            discord.SelectOption(label="18,BAN保証", value="sub18"),
            discord.SelectOption(label="19,永久BAN保証", value="sub19"),
            discord.SelectOption(label="20,永久猫缶補充", value="sub20")
        ]
        super().__init__(placeholder="適用する項目をすべて選んでください...", min_values=1, max_values=len(options), options=options)

    async def callback(self, interaction: discord.Interaction):
        # 選択された依頼内容をカンマ区切りの文字列にして Modal に渡す
        selected_values = ",".join(self.values)
        await interaction.response.send_modal(PurchaseModal2(selected_values))

class PanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @ui.button(label="購入(1-14番)", style=discord.ButtonStyle.success, custom_id="persistent_panel_buy")
    async def buy_button(self, interaction: discord.Interaction, button: ui.Button):
        view = ui.View()
        view.add_item(OrderSelect())
        await interaction.response.send_message("依頼内容を選択してください", view=view, ephemeral=True)

    @ui.button(label="購入(15-20番)", style=discord.ButtonStyle.success, custom_id="persistent_panel_buy2")
    async def buy_button2(self, interaction: discord.Interaction, button: ui.Button):
        view = ui.View()
        view.add_item(OrderSelect2())
        await interaction.response.send_message("依頼内容を選択してください", view=view, ephemeral=True)
        

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        
    async def setup_hook(self):
        self.add_view(PanelView())
        self.add_view(TicketView())
        await self.tree.sync()

bot = MyBot()

@bot.tree.command(name="にゃんこ大戦争代行", description="にゃんこ大戦争パネル設置")
@app_commands.describe(category="チケットを作成するカテゴリ", staff_role="送金確認ができるスタッフのロール")
@app_commands.checks.has_permissions(administrator=True)
async def setup_panel(interaction: discord.Interaction, category: discord.CategoryChannel, staff_role: discord.Role):
    # カテゴリとロールのIDをコンフィグに保存
    config[str(interaction.guild.id)] = {"category": category.id, "role": staff_role.id}
    save_config(config)

    description = (
        "にゃんこ大戦争自動代行\n"
        "引き継ぎコードと認証コードに間違いがないようにしてください\n"
    )

    embed = discord.Embed(title="にゃんこ大戦争自動代行", description=description, color=0x2b2d31)
    embed.add_field(name="1.猫缶 58000", value="> 150円", inline=False)
    embed.add_field(name="2.XPカンスト", value="> 400円", inline=False)
    embed.add_field(name="3.レアチケットカンスト", value="> 400円", inline=False)
    embed.add_field(name="4.にゃんチケットカンスト", value="> 200円", inline=False)
    embed.add_field(name="5.プラチナチケット", value="> 500円", inline=False)
    embed.add_field(name="6.レジェンドチケットカンスト", value="> 500円", inline=False)
    embed.add_field(name="7.NPカンスト", value="> 300円", inline=False)
    embed.add_field(name="8.リーダーシップ", value="> 500円", inline=False)
    embed.add_field(name="9.戦闘アイテムカンスト", value="> 400円", inline=False)
    embed.add_field(name="10.全キャラ解放", value="> 400円", inline=False)
    embed.add_field(name="11.エラーキャラ削除", value="> 200円", inline=False)
    embed.add_field(name="12.全ステージ解放", value="> 200円", inline=False)
    embed.add_field(name="13.キャッツアイ", value="> 500円", inline=False)
    embed.add_field(name="14.イベントチケット", value="> 500円", inline=False)
    embed.add_field(name="15.指定キャラ第3形態1体につき", value="> 150円", inline=False)
    embed.add_field(name="16.ステージ進行 1編につき", value="> 600円", inline=False)
    embed.add_field(name="17.マタタビ全種類カンスト", value="> 800円", inline=False)
    embed.add_field(name="18.BAN保証", value="> 500円", inline=False)
    embed.add_field(name="19.永久BAN保証", value="> 5000円", inline=False)
    embed.add_field(name="20.永久猫缶補充", value="> 3000円", inline=False)
    await interaction.channel.send(embed=embed, view=PanelView())
    await interaction.response.send_message(f"パネルを設置しました。\nカテゴリ: {category.name}\nロール: {staff_role.name}", ephemeral=True)

@bot.event
async def on_ready():
    bot.add_view(PersistentLoginView())
    print(f"ログインしました: {bot.user}")

if TOKEN:
    bot.run(TOKEN)
else:
    print("ログインエラー")
