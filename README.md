# Kripto Piyasa Analiz Botu

Bu bot otomatik islem yapmaz. BTC ve secili altcoin sepeti icin piyasa verilerini analiz eder ve Telegram'a kisa giris karari gonderir.

> Bot borsada islem acmaz; karar raporunu gonderir, islemi kullanici manuel yapar.

## Ozellikler

- Binance public API ile BTC, Layer 1, Layer 2, AI, RWA ve DePIN sembollerini izler.
- Kisa, orta ve ana trend verilerini icerde analiz eder.
- RSI, EMA 20/50/200, MACD, Bollinger, momentum ve hacim degisimini hesaplar.
- Trend yonunu EMA dizilimi, EMA egimi, MACD ivmesi, hacim ve swing yapisina gore skorlayarak yorumlar.
- Zaman dilimlerini icerde analiz eder; Telegram'da `Giriş: EVET`, `Giriş: HAYIR` veya `Giriş: BEKLE` yazar.
- Binance taker alis/satis baskisi ve order-book duvarlarini izleyerek fake yukselis / dagitim riskini ayirmaya calisir.
- `CRYPTOQUANT_API_KEY` eklenirse Binance'e BTC/ETH net giris-cikis ve stablecoin rezerv degisimini rapora ekler.
- Spot hesap icin long/short dili kullanmadan momentum ve risk ozeti verir.
- BTC ana trendi zayifsa altcoinlerde giris otomatik kapatilir.
- BTC zayiflama, anlik para cikisi ve hacimli satis birlesirse `Cokus erken uyari` verir.
- Duzeltme geldiginde para USDT'de mi bekliyor, sektor icinde mi donuyor, baska projeye mi kayiyor ayirmaya calisir.
- Anlik para akisini Binance taker alis/satis hacmi ve net fark olarak yazar; kucuk miktarlari `zayif/cok zayif` diye etiketler.
- Altcoin secimi anlik Binance USDT hacmi + fiyat degisimine gore yapilir; teyit verisi icerde kullanilir.
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
- `telegram_setup.py`: Bot icin chat id bulma yardimcisi.
- `main.py`: Saatlik calisma dongusu.
- `.env.example`: Ornek Telegram ayarlari.
- `deploy_to_vps.sh`: Botu VPS'e tasima yardimcisi.
- `vps_install_service.sh`: VPS uzerinde 5 dakikalik systemd timer kurar.

## Kurulum

Bu bot ek Python paketi istemez; standart kutuphanelerle calisir.

1. `.env.example` dosyasini `.env` olarak kopyala.

```bash
cp .env.example .env
```

2. `.env` dosyasini doldur.

```bash
TELEGRAM_BOT_TOKEN=bot_token
TELEGRAM_CHAT_ID=chat_id
CRYPTOQUANT_API_KEY=
BTCUSDT_COST=
ETHUSDT_COST=2049
SOLUSDT_COST=
FETUSDT_COST=
RENDERUSDT_COST=
ONDOUSDT_COST=
FILUSDT_COST=
ARBUSDT_COST=
OPUSDT_COST=
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

## Rapor Ornegi

```text
KRIPTO RAPORU
2026-05-31 00:15:00 CEST
Cokus erken uyari ⚠️ BTC trend zayif | para cikisi | hacimli satis
Anlık Net Akış: Layer 1 +420K$ | küçük (ETH +310K$) | Alış 4.2M$ / Satış 3.8M$
Düzeltme: Para coinlerden çıkıp USDT tarafında bekliyor

BTCUSDT
Sektor: BTC
Fiyat: 73896
Giriş: EVET 🟢 | zincir destekli
Zincir: Binance net çıkış -1.2K BTC ✅
İz: Alıcı %56 | OB alış duvarı
Yükseliş sinyali var 🟢
Akış: Alış 1.2M$ | Satış 1.1M$ | Net +82K$ zayıf | teyit -0.2%
Beklenti: yukarı deneme | hedef 78080
Seviye: 72512 / 78080
Plan: Giriş 72512-73238 | Çıkış/Hedef 78080 | Risk 71425 altı
Alarm: Mum: Hammer

ETHUSDT
Sektor: Layer 1
Fiyat: 2024
Giriş: HAYIR 🔴 | fake/dağıtım riski
Zincir: Binance net giriş +8.4K ETH ⚠️
İz: Satıcı %57 | Dağıtım riski | Fake YÜKSEK ⚠️
Akış: Alış 830K$ | Satış 866K$ | Net -36K$ zayıf | teyit -0.4%
Beklenti: aşağı baskı | destek 1967
Seviye: 1967 / 2140
Plan: Giriş yok | Tetik 2140 üstü | Risk 1938 altı
Alarm: Yok
```
## Notlar

- Binance public API kullanildigi icin API key gerekmez.
- Telegram icin BotFather'dan bot token alman gerekir.
- Chat ID icin kendi Telegram hesabina veya gruba botu ekleyip chat id kullanmalisin.
- Akış satiri `Anlık Net Akış`, `Teyit Akışı` veya `24s Akış` diye gelir; miktar kucukse sinyali buyutmemek icin `zayif/cok zayif` yazar.
- `market_bot.py` onceki kisa piyasa yonu ve piyasa akisi yardimci aracidir; yeni moduler botun ana girisi `main.py` dosyasidir.
