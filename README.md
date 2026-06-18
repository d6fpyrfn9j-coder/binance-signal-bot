# Kripto Piyasa Analiz Botu

Bu bot otomatik islem yapmaz. BTC ve secili altcoin sepeti icin piyasa verilerini analiz eder ve Telegram'a kisa giris karari gonderir.

> Bot borsada islem acmaz; karar raporunu gonderir, islemi kullanici manuel yapar.

## Ozellikler

- Binance public API ile BTC, ETH, SOL ve likit Layer 1, Layer 2, AI, RWA, DePIN adaylarini izler.
- Kisa, orta ve ana trend verilerini icerde analiz eder.
- RSI, EMA 20/50/200, MACD, Bollinger, momentum ve hacim degisimini hesaplar.
- Trend yonunu EMA dizilimi, EMA egimi, MACD ivmesi, hacim ve swing yapisina gore skorlayarak yorumlar.
- Zaman dilimlerini icerde analiz eder; Telegram'da `Giriş: EVET`, `Giriş: HAYIR` veya `Giriş: BEKLE` yazar.
- Futures modu acikken Telegram'da `Futures: LONG`, `Futures: SHORT` veya `Futures: BEKLE` yazar; guven, tetik, hedef, stop ve R/R seviyesini ayri verir.
- Futures sinyallerini `futures_signal_history.json` icinde ayri takip eder; son 100 futures sonucu, kacan firsat, fake tetik, korunan zarar ve stop sayisini rapora yazar.
- Gunluk trade icin yakin seviyelerden `Giriş`, `Tetik`, `Hedef`, `Stop` ve `R/R` hesaplar.
- Piyasa modu, guven puani, BTC dominance, haber filtresi, fake pump, balina akisi ve sinyal basari testini kisa yazar.
- `R/R` rengi guven puanina baglidir; guven kirmiziyken R/R yesil yanmaz.
- Tetik seviyesi sadece fitil/dokunma degildir; bot 15m kapanis teyidi arar.
- Bot kendi Telegram sinyallerini Binance 15m mumlariyla karsilastirir; fake tetik, korunan zarar, stop ve kacan firsat sayar.
- `backtester.py` gecmis Binance mumlariyla gercek backtest yapar; en iyi guven/RR esiklerini ve coin agirliklarini `signal_weights.json` dosyasina yazar.
- `signal_weights.json` varsa bot bu optimize agirliklari otomatik kullanir.
- Verilen giriş/çıkış seviyelerini son gerçek Binance mum aralığıyla karşılaştırıp `Gerçek` satırıyla sonucu yazar.
- Binance taker alis/satis baskisi ve order-book duvarlarini izleyerek fake yukselis / dagitim riskini ayirmaya calisir.
- Binance disinda Coinbase, Kraken, OKX, Bybit, KuCoin ve MEXC public ticker verisiyle fiyat/hacim teyidi yapar.
- Render calismasinda mesajdan once 180 saniye boyunca order book ve gerceklesen alis/satis akisini tarar; rapor tek anlik goruntuye dayanmaz.
- WebSocket worker modunda bot surekli acik kalir, Binance `aggTrade` ve `depth20` streamlerinden akisi toplar, Telegram'a 5 dakikada bir rapor yollar.
- `CRYPTOQUANT_API_KEY` eklenirse once tum borsalar, olmazsa Binance icin BTC/ETH net giris-cikis ve stablecoin rezerv degisimini rapora ekler.
- Borsaya net coin girisi satis riski, borsadan net coin cikisi toplama/soguk cuzdan izi olarak yorumlanir.
- Ucretsiz haber filtresi ETF, FED, hack ve dava basliklarini tarar; ciddi haber riski varsa AL sinyalini dusurur.
- `signal_history.json` icinde AL sinyallerini saklar; 1 saat / 4 saat performansi ve son 100 sinyal basari oranini takip eder.
- Spot hesap icin long/short dili kullanmadan momentum ve risk ozeti verir; futures modu icin long/short yonu uretir.
- BTC ana trendi zayifsa altcoinlerde giris otomatik kapatilir.
- BTC zayiflama, anlik para cikisi ve hacimli satis birlesirse `Cokus erken uyari` verir.
- Duzeltme geldiginde para USDT'de mi bekliyor, sektor icinde mi donuyor, baska projeye mi kayiyor ayirmaya calisir.
- Anlik para akisini Binance taker alis/satis hacmi ve net fark olarak yazar; kucuk miktarlari `zayif/cok zayif` diye etiketler.
- Altcoin havuzu genistir; raporda en fazla 10 coin kalir. Bot 5dk para akisi, 1s teyit, 24s likidite ve momentumla en guclu 7 altcoini secer.
- Bos/dusuk likiditeli coinleri elemek icin varsayilan altcoin filtresi 24s hacim `20M$`, 5dk hacim `50K$`, 1s hacim `250K$` altini rapora almaz.
- Destek/direnc seviyelerini onceki swing high/low uzerinden belirler; son mum kirilim yaptiysa bunu daha net yakalar.
- Riskli durumlarda uyari verir:
  - Asiri RSI
  - Ani hacim artisi
  - EMA20/EMA50 kesişimi
  - Bollinger bant temasi
  - Momentum gucu/zayifligi
  - Doji, Hammer, Engulfing mumlari
  - Buyuk alis/satis izi
  - Fake yukselis ve dagitim riski
- Telegram Bot API ile rapor gonderir.
- Varsayilan olarak her 5 dakikada bir otomatik calisir.
- `.env` dosyasindan `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` okur.
- Istersen `.env` icinden maliyet seviyelerini okuyup fiyata uzakligi yazar.
- Hata yakalama ve `bot.log` dosyasina loglama vardir.

## Dosya Yapisi

- `data_fetcher.py`: Binance mum verilerini ceker.
- `indicators.py`: RSI, EMA, MACD ve ortalama hesaplari.
- `analyzer.py`: Trend, destek/direnc ve risk uyarilari.
- `telegram_sender.py`: Telegram mesaj gonderimi.
- `websocket_worker.py`: Binance WebSocket akisini surekli izleyen worker.
- `backtester.py`: Gecmis Binance mumlariyla backtest ve otomatik agirlik optimizasyonu.
- `signal_weights.py`: Optimize agirlik dosyasini okur.
- `telegram_setup.py`: Bot icin chat id bulma yardimcisi.
- `main.py`: Saatlik calisma dongusu.
- `.env.example`: Ornek Telegram ayarlari.
- `deploy_to_vps.sh`: Botu VPS'e tasima yardimcisi.
- `vps_install_service.sh`: VPS uzerinde 5 dakikalik systemd timer kurar.

## Kurulum

Botun ana kismi standart kutuphanelerle calisir; WebSocket worker icin `requirements.txt` icindeki paket kurulur.

1. `.env.example` dosyasini `.env` olarak kopyala.

```bash
cp .env.example .env
```

2. `.env` dosyasini doldur.

```bash
TELEGRAM_BOT_TOKEN=bot_token
TELEGRAM_CHAT_ID=chat_id
CRYPTOQUANT_API_KEY=
CRYPTOQUANT_EXCHANGES=all_exchange,binance
MULTI_EXCHANGE_ENABLED=true
MULTI_EXCHANGE_LIST=binance,coinbase,kraken,okx,bybit,kucoin,mexc
REPORT_TIMEZONE=Europe/Brussels
BTCUSDT_COST=
ETHUSDT_COST=2049
CORE_SYMBOLS=BTCUSDT,ETHUSDT
ALT_CANDIDATES=
ENABLE_ALTCOINS=false
SOLUSDT_COST=
BNBUSDT_COST=
XRPUSDT_COST=
ADAUSDT_COST=
AVAXUSDT_COST=
LINKUSDT_COST=
SUIUSDT_COST=
APTUSDT_COST=
NEARUSDT_COST=
DOTUSDT_COST=
FETUSDT_COST=
RENDERUSDT_COST=
GRTUSDT_COST=
ONDOUSDT_COST=
PENDLEUSDT_COST=
FILUSDT_COST=
ARBUSDT_COST=
OPUSDT_COST=
STRKUSDT_COST=
MANTAUSDT_COST=
MAX_REPORT_SYMBOLS=2
MIN_ALT_QUOTE_VOLUME=20000000
MIN_ALT_5M_QUOTE_VOLUME=50000
MIN_ALT_1H_QUOTE_VOLUME=250000
SIGNAL_AUDIT_WINDOW_SECONDS=14400
PERFORMANCE_LEARNING_ENABLED=true
QUALITY_LOW_24H_VOLUME=30000000
QUALITY_LOW_FLOW_VOLUME=75000
SIGNAL_WEIGHTS_ENABLED=true
SIGNAL_WEIGHTS_FILE=signal_weights.json
FUTURES_MODE_ENABLED=true
FUTURES_MIN_CONFIDENCE=72
FUTURES_MIN_EDGE=8
FUTURES_MIN_RR=1.8
FUTURES_TRACKING_ENABLED=true
FUTURES_SIGNAL_HISTORY_FILE=futures_signal_history.json
```

Chat ID bilmiyorsan:

1. Telegram'da botuna `test` yaz.
2. Su komutu calistir.

```bash
python3 telegram_setup.py
```

3. Cikan `CHAT_ID=...` degerini `.env` icindeki `TELEGRAM_CHAT_ID` alanina yaz.

CryptoQuant kullanacaksan `CRYPTOQUANT_API_KEY` alanina access token ekle. Eklenmezse bot zincir ustu veriyi atlar ve Binance verisiyle calismaya devam eder.

3. Tek sefer test et.

```bash
python3 main.py --once
```

Telegram'a gondermeden sadece terminalde gormek icin:

```bash
python3 main.py --once --no-telegram
```

Backtest ve otomatik optimizasyon icin:

```bash
python3 backtester.py --days 30 --sample-every 8
```

Bu komut:

- `backtest_results.json` icine detayli gecmis test sonucunu yazar.
- `signal_weights.json` icine botun kullanacagi optimize guven/RR ve coin agirliklarini yazar.

GitHub Actions icindeki `Backtest Optimize` workflow'u her gun otomatik calisir ve `signal_weights.json` degisirse repo'ya commit eder.

4. 5 dakikada bir calistir.

```bash
python3 main.py
```

Terminal kapaninca da calismasi icin macOS servisi olarak kur:

```bash
./install_launch_agent.sh
```

Kaldirmak icin:

```bash
./uninstall_launch_agent.sh
```

Farkli aralik icin:

```bash
python3 main.py --interval 1800
```

## VPS'e Tasima

VPS Ubuntu/Debian ise botu Mac kapali olsa bile calistirmak icin:

```bash
./deploy_to_vps.sh root@VPS_IP
```

Kurulum sonunda VPS uzerinde systemd timer aktif olur ve bot her 5 dakikada bir calisir.

Durum kontrolu:

```bash
systemctl status binance-signal-bot.timer --no-pager
```

Log kontrolu:

```bash
journalctl -u binance-signal-bot.service -n 80 --no-pager
```

## GitHub Actions ile 5 Dakikada Bir Calistirma

Mac kapali olsa bile GitHub botu calistirsin istiyorsan repo icinde su workflow hazir:

```text
.github/workflows/binance-signal-bot.yml
```

GitHub'da repo actiktan sonra `Settings > Secrets and variables > Actions > New repository secret`
bolumune sunlari ekle:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

Maliyet yazmak istersen opsiyonel olarak bunlari da secret olarak ekleyebilirsin:

```text
BTCUSDT_COST
ETHUSDT_COST
SOLUSDT_COST
FETUSDT_COST
RENDERUSDT_COST
ONDOUSDT_COST
FILUSDT_COST
ARBUSDT_COST
OPUSDT_COST
```

Workflow her 5 dakikada bir calisir. Manuel test icin GitHub'da:

```text
Actions > Binance Signal Bot > Run workflow
```

## Render WebSocket Worker

Kesintisiz takip icin `render.yaml` icinde `binance-signal-worker` servisi hazirdir.

Worker mantigi:

```text
WebSocket surekli acik
Order book + alis/satis akisi hafizada
Her 5 dakikada Telegram raporu
```

Kullanilan streamler:

```text
<symbol>@aggTrade
<symbol>@depth20@1000ms
```

Worker canli dogrulandiktan sonra eski cron servis durdurulabilir; boylece cift mesaj gelmez.

## Rapor Ornegi

```text
KRIPTO RAPORU
2026-05-31 00:15:00 CEST
PİYASA MODU: 🟡 DİKKAT | Güven 58/100 🟡
Son 100 sinyal: %62 🟢 (31/50)
BTC.D: 61.4% | Haber: ETF giriş var 🟢 | FED sakin 🟢 | Haber büyük risk yok 🟢
Para: çıkış 🔴 -420K$ | Alış 3.8M$ / Satış 4.2M$

BTCUSDT
Fiyat: 73896
Karar: GİRİŞ VAR 🟢 | zincir destekli
Güven: 74/100 🟢 | R/R 1:3.1 🟢
Para: +82K$ alış 🟢
Büyük emir: Alış 1.4M$ / Satış 860K$ 🟢
Fake pump: Düşük 🟢
🚀 Ekstra yükseliş yakın
Giriş: 72512-73238 🟢
Hedef: 78080
Stop: 71425
Gerçek: kârda ilerliyor 🟢 | hedef 78080
Test: 1s +0.8% | 4s ? | Açık 🟡
Koruma: risk -1.2% | ödül +3.5%
Uyarı: Mum: Hammer ⚠️

ETHUSDT
Fiyat: 2024
Karar: GİRİŞ YOK 🔴 | fake/dağıtım riski
Güven: 38/100 🔴 | R/R 1:0.8 🔴
Para: -36K$ satış 🔴
Büyük emir: Alış 220K$ / Satış 610K$ 🔴
Fake pump: Yüksek 🔴
Giriş: Bekle 🟡
Tetik: 15m kapanış 2140 üstü 🟡
Geri çekilme: 1967-1983
Hedef: 2180
Stop: 1938 (tetikten sonra)
Gerçek: tetik gelmedi | hedef 2180
Koruma: işlem yok, zarar korunur 🟢
Uyarı: Yok 🟢
```
## Notlar

- Binance public API kullanildigi icin API key gerekmez.
- Render cron mesajlari 5 dakikada bir gonderir; komut rapordan once `--pre-scan-seconds 180` ile piyasa akisini toplar.
- REST API gecikmesine gore ornek sayisi degisir. Birebir saniyelik ve kesintisiz order book icin sonraki seviye Binance WebSocket worker kurulumudur.
- WebSocket worker aktifse veri boslugu kalmaz; sadece Telegram raporu 5 dakikada bir gelir.
- Telegram icin BotFather'dan bot token alman gerekir.
- Chat ID icin kendi Telegram hesabina veya gruba botu ekleyip chat id kullanmalisin.
- Akış satiri `Anlık Net Akış`, `Teyit Akışı` veya `24s Akış` diye gelir; miktar kucukse sinyali buyutmemek icin `zayif/cok zayif` yazar.
- `Borsa teyidi` satiri Binance disi borsalarda fiyat ayrismasi var mi gosterir; spread yuksekse sinyal guveni duser.
- Zincir satiri kesin niyet okumaz; borsaya giris satis riski, borsadan cikis toplama ihtimali olarak yorumlanir.
- Haber filtresi ucretsiz RSS basliklarindan risk etiketi uretir; kesin ETF dolar akisi icin ayrica profesyonel veri API'si gerekir.
- Render dosya sistemi servis yeniden deploy olunca sifirlanabilir; uzun vadeli sinyal istatistigi icin sonraki adim kalici veritabani eklemektir.
- Futures performans takibi worker calisirken `futures_signal_history.json` icinde birikir; GitHub Actions tek seferlik ortamda calistigi icin bu hafiza kalici olmayabilir.
- `market_bot.py` onceki kisa piyasa yonu ve piyasa akisi yardimci aracidir; yeni moduler botun ana girisi `main.py` dosyasidir.
