## **1. Best GitHub repositories for horizontal support/resistance levels**

I found the following projects worth investigating.

[Detection of price support and resistance levels in Python | by Gianluca Malato | TDS Archive | Medium](https://images.openai.com/static-rsc-4/pRQ3O6IfA1MfT0-8pwU15PLjAMqZzJ9KSw5ctTCKXNpvLYYihWZOluIHkzYgZybzdfGyBbUQ_8e83e97-qKliH70UVSQeaW11aVmdUcQfzKUJMJdv7IK7t6izNvE6QXFHwOFe2Sw-dGRG71FH9aamCyA3FK3sMy_4BLfo4LsIrk?purpose=inline)

**1. day0market/support_resistance**

My first choice

Python · Horizontal levels · Clustering

[GitHub repository](https://github.com/day0market/support_resistance?utm_source=chatgpt.com)

- Identifies swing highs and lows.
- Uses ZigZag pivot detection.
- Groups nearby prices using Agglomerative Clustering.
- Supports level scoring based on touches, cuts and nearby pivots.
- Provides candlestick visualisation.

[Everyday Trader hears the same thing over and over again...  "Price Action" "Volume Analysis" "EMA &... - Mohit Sharma | Rattibha](https://images.openai.com/static-rsc-4/Sd2uSPzLLEAm90XT0Z96gon976fYn5oEp6_XE_EHcei93VmGMFXlga9_EcMJ9tg-4n3zh8U6nDulaHozYv1mZpBtzwci2UcEJXqxKTvMOg5PIsSkDJYRjqEzt2diOAITXItzleO6padlH0-79vUw83Goy7dGiYDUYc-xb2Y_z4M?purpose=inline)

**2. xucian/py.trading.support_resistance**

Strong candidate

Python · Price clustering · Level scoring

[GitHub repository](https://github.com/xucian/py.trading.support_resistance?utm_source=chatgpt.com)

- Uses two approaches: ZigZagClusterLevels and RawPriceClusterLevels.
- Groups nearby pivot prices into horizontal levels.
- Allows configurable clustering distance.
- Has a TouchScorer to evaluate level quality.
- Supports mean or median level calculation.

[NEW V1.3 (India)](https://images.openai.com/static-rsc-4/XBaV2NOSzywjTAOucrQlpxhM3pnqNlA-HE7NPXhISZ-KlL9cdyZ6FAXfhsZLh3CF3-DzhW01IdratWEqB5zXzH1ZPT-snr9G3UWevIBC0K8j2k3nmUbwO9Zjt1itPsoJq6ildmwV7y9CQw0A5ErFI0MrG21sIP1qfXxqCCxXIhg?purpose=inline)

**3. VolodymyrFilias/feels-indicators**

Interesting methodology

Pine Script v6 · Support/resistance zones

[GitHub repository](https://github.com/VolodymyrFilias/feels-indicators?utm_source=chatgpt.com)

- Clusters pivots using ATR-based distance.
- Requires multiple confirmed touches.
- Includes local, swing and major structure presets.
- Has confluence pivot zones and strength scoring.
- Tracks historical hit/expiry outcomes.

Particularly useful for understanding how to distinguish minor levels from major levels.

[This Strategy Made $11,000 Trading Less Than 15 Minutes Per Day: My Complete Trading System Revealed | by FXM Brand (Stephen) | Feb, 2026 | Medium](https://images.openai.com/static-rsc-4/qH6dIKRzWrmvGicO0OaGVDw2UeLaDW8trJLdP1Ri9cgVttMqby2Rylz7g1fqX-qDC-oC14DgQBhIuLljAZ1foffZTNjhpAp8_Sdlibo0hisBx-EeTL6h6XSfFplwLdn6qXTR4wluTsWnyJYFdan0gOks1EImlI_OOlTlJfuhenQ?purpose=inline)

**4. ednunezg/pytrendline**

Python · Trendline detection

[GitHub repository](https://github.com/ednunezg/pytrendline?utm_source=chatgpt.com)

- Detects support and resistance trendlines.
- Identifies pivot highs and lows.
- Detects line breakouts.
- Has configurable scoring and plotting.

Not directly suited to horizontal levels, but useful for a future diagonal trendline scanner.

[SR Zones and Timeframe Levels — Indicator by smugus — TradingView](https://images.openai.com/static-rsc-4/nYTRwV_rmP2carohrMbtvSovXc9BwQ-T9SRtO2xDenjHAJ_myhInh_wxPXGFJFH5D6KrGYsiWEopVgVPXxkZI_BKcfml6c__zPwvetPTqSKgLAN0NECpvSPkAfTT9dXsVylmd5hcEVWsgHt89xf8cj8LfRoiqQ2jMq9XJFH77mk?purpose=inline)

**5. VolodymyrFilias/feels-indicators – Confluence Pivot Zones**

Pine Script · Zone confluence and signals

[Indicator source files](https://github.com/VolodymyrFilias/feels-indicators/tree/main/indicators?utm_source=chatgpt.com)

This is the same repository as option 3, but its confluence indicator deserves separate attention for its zone scoring and signal-generation methodology.

Another useful reference is [PlasmaZero/Support-and-Resistance-Indicator](https://github.com/PlasmaZero/Support-and-Resistance-Indicator?utm_source=chatgpt.com). It includes pivot-based zones, volume analysis, strength scoring and break-and-retest alerts.

The first two Python projects are particularly relevant because they work directly with OHLC data and produce numerical price levels, which is what a stock screener needs.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

GitHub+4



## **2. What exactly should we screen for?**

I would not limit the scanner to simply identifying support and resistance lines. We should identify different stages of price interaction with these levels.

I suggest the following six categories.

[Chứng khoán tuần: Cơ hội cho VN-Index vượt đỉnh lịch sử trong tầm tay  | Thời báo Tài chính Việt Nam](https://images.openai.com/static-rsc-4/2HdorpFcctx23khnbceJrKT2GfSpCKkpfoRT0i001ydK0zDVhhX_Z5-gfWiF2KYAeC5eM8BLNbdbawA32wCwgASFrFtRdij3a76tkGmn35CqO4mu1IV66iTrsqUwfIzCC_pJqOlTk45yRo4SDyZrqKYF7egaRKly6hMKqQ6Ca94?purpose=inline)

**A. Near Resistance**

Potential breakout

Stock is approaching a historically significant resistance level.

Example: Price is within 1% of resistance.

[Trade with Support and Resistance: A Comprehensive Guide](https://images.openai.com/static-rsc-4/4NbFd5ubKL9Mi4isuGOekxX97Y05BS9QetfZjTpe5KfUM7DqgnXXfKcX137LEXTjm4_TGjLX_19DqM_gzcl4kHZBu0O9hlFSN4P0QGU4alFko6Mn7b-MpEuvO5ZG1kgpJ3QP_kw1L1uYMXW233Kaobb0zV_9DdCqLS2ao-l22Ic?purpose=inline)

**B. Near Support**

Potential reversal

Stock is approaching a historically significant support level.

Example: Price is within 1% of support.

[Indicador de Suporte e Resistência no Trading / Axi](https://images.openai.com/static-rsc-4/reGZwsnhm2NKX3foLxJUyHmKzF1t7dEhlFv83z0_oyd602hatQ6kZ3vk24sxRrBfb01jjyzFezOPzVDS7mbmwPn6wkABGrwIqPbk5mMU9HrzJYLgdNuFCo7qJM0vzvQKAQhMV8zt1WHB4BaNUTYEpnYAqoYRWy_5efjK050bWJs?purpose=inline)

**C. Support Bounce**

Bullish setup

Price touches a support zone and subsequently moves upward.

Look for bullish candle confirmation and volume expansion.

[Ithy - Unlocking Forex Profits: Your Detailed Guide to Finding High-Impact Key Levels](https://images.openai.com/static-rsc-4/xOpfgqJLOgdqsHTwTl-9yrfCBHZLsbUq9NFoJsaRr0erNl6yS_89gucfquIdngFHoapsJhMDh5i7-5qQnpYqNgLQf7qTc_Pc6s19QKO_hWL2i8PhkbAonT3fmGdZukxEMzrSW5RngFcbNCZNwB0QeKJ2Uthi6I3hSQMyh_GsJYw?purpose=inline)

**D. Resistance Rejection**

Bearish setup

Price tests resistance but fails to sustain above it and moves downward.

[Breakout Trading Strategy: Complete Guide 2025 | SignalWavesAI](https://images.openai.com/static-rsc-4/apCBrPeurE4rn7OnfLsX1ooDfhaYDn022eYAzo_nr7LkcOc2uhZ2jNqzOtCVTVkQmxfSJCImxa6RO6yMuML5wZ8JZTauxs3j1dvWaBJOhgEa_LU8xIjm_6Z3VNCJq5uCqVY9Xhysf_1PirkWAzHWcCKkMVJs86lhmDtpgVd0KDk?purpose=inline)

**E. Resistance Breakout**

Bullish breakout

Price closes above a previously established resistance zone, preferably with increased volume.

[GOGO - GOGO Breaks Below $6 Support - Technical Breakdown Analysis](https://images.openai.com/static-rsc-4/WuYYjCjTAXqizUYvLTeW0aUag6anw3gt6yx3ZvAS7uOV4XeI_Nr929gWEQpLdI_ACS_93CIDuQpg-f3J-RLWVQeZSVRFb_SHtiK_9hUqwmUCn0O9m8w08EgXB6kHke3Lcpo8aS8HGL4iXiHI52LxIc0W4f6B1Kq5DzW8Wm_ienI?purpose=inline)

**F. Support Breakdown**

Bearish breakdown

Price closes below a previously established support zone.

I would additionally include **breakout retests**. These are particularly interesting: a stock breaks resistance, comes back to test the same level, and then resumes its upward movement.

## **3. How I would build the detection engine**

The most important part is not drawing horizontal lines. It is deciding which price levels deserve to be considered significant.

I recommend combining three techniques.

### **Technique 1: Pivot clustering**

This is the approach used by the first two GitHub repositories.

Consider the following historical swing highs:

| Date   | Swing high |
| ------ | ---------- |
| 10 Jan | ₹1,020     |
| 25 Jan | ₹1,026     |
| 14 Feb | ₹1,018     |
| 8 Mar  | ₹1,023     |
| 22 Mar | ₹1,025     |

These prices are clustered around ₹1,022.

Instead of treating them as five different resistance levels, the algorithm identifies one resistance zone.

The same process applies to swing lows for support.

### **Technique 2: Level strength scoring**

Not every level is equally important.

I would calculate a strength score using:

- Number of historical touches.
- Number of independent price reversals.
- Time elapsed since the first touch.
- Recency of the last touch.
- Number of successful bounces.
- Number of failed attempts to break the level.
- Trading volume around the level.
- Whether the level is also visible on a higher timeframe.

We should be careful not to count multiple consecutive candles within one test as independent touches.

### **Technique 3: ATR-based zones**

A fixed ₹5 distance between two price points means different things for a ₹100 stock and a ₹2,000 stock.

Therefore, use ATR (Average True Range) or percentage-based distances to cluster price points.

This makes the algorithm adaptive across different stocks and volatility regimes.

**My proposed implementation:**

## **AlgoDesk – Support & Resistance Scanner**

**Existing Market Data Layer**

DataService · Historical OHLCV · NSE/BSE

**Support & Resistance Detection Engine**

Pivot identification · Price clustering · Zone generation

**Level Scoring**

Touches, volume, recency, strength

**Price Interaction**

Bounce, breakout, rejection, retest

**Screen Plugin**

Universe filtering · Ranking · Classification

**Screening Results**

CSV / JSON · CLI · Charts · Historical backtesting

## **5. One important issue: Avoiding look-ahead bias**

This is particularly important because you intend to use AlgoDesk for backtesting as well.

Suppose a pivot low occurs on Monday.

If our algorithm uses five candles on the right to confirm that pivot, the pivot cannot be considered known on Monday. It only becomes confirmed after those five candles have completed.

Similarly:

- A breakout should be identified using completed candles.
- A bounce should only be classified after the reversal is confirmed.
- A level's historical strength must use information available at the time of screening.
- Corporate actions and adjusted historical prices must be handled consistently.

Otherwise, the scanner might look excellent in historical testing but produce misleading results in actual trading.

## **6. My recommended development approach**

I would not blindly copy any one repository.

| Component                      | Recommended starting point    |
| ------------------------------ | ----------------------------- |
| Pivot detection                | day0market/support_resistance |
| Horizontal level clustering    | day0market + xucian           |
| Level strength scoring         | xucian TouchScorer, extended  |
| ATR-based clustering           | feels-indicators methodology  |
| Bounce/breakout classification | Develop our own               |
| Retest detection               | Develop our own               |
| Backtesting and validation     | Existing AlgoDesk engine      |

One qualification: these are algorithmic starting points, not verified production-ready dependencies. We should inspect their current source, licensing, dependency compatibility and tests before adopting code.

# **Implementation Plan**

# **AlgoDesk – Support & Resistance Scanner**

Design proposal

Level classification

**Major + Minor**

Universe

**Custom instrument groups**

Output

**CLI table + saved results**

Execution

**Research / Backtest**

# **Part 1. Repository integration assessment**

## **1.1 What already exists and what we can reuse**

This is the most important part of the design. We should reuse existing infrastructure rather than introduce parallel implementations.

# **Part 2. Functional and algorithmic design**

## **2.1 Overall detection methodology**

I recommend a hybrid algorithm combining:

1. Fractal pivot detection.
2. ATR-based price clustering.
3. Level strength scoring.
4. Multi-timeframe confluence.
5. Price interaction state classification.

Why not simply use ZigZag?

ZigZag is useful for identifying swing points, but its most recent pivots can change as new prices arrive. That can introduce repainting and historical look-ahead problems.

We should use confirmed fractal pivots as the primary method. ZigZag can remain an optional comparison method during research.

## **2.2 Stage 1: Identify swing highs and swing lows**

Consider a sequence of candles.

A swing high is identified when its high is greater than the highs of a specified number of candles on both sides.

A swing low is the reverse.

[What is ICT Swing High: 2 Easy Steps + 2 Examples - Smart Money ICT](https://images.openai.com/static-rsc-4/MmneJQgiOBm-5piON3GMZ5b9120BGR6jzoKIrww2hp_tndTFkYUzr28sfaLWLXaTM0e7NSeQ12itNT6SnCp2O-i0UvuyDXVoYQlzJYZsr135rsMiTieKzhgLEsxKqwfbGb6BvEIzhuGPryQ14jxMfZlPdjGHNHLngLYgAJ3SpnE?purpose=inline)

[smartmoneyict.com](https://smartmoneyict.com/ict-swing-high/?utm_source=chatgpt.com)

Proposed default configuration:

| Parameter                | Initial value     |
| ------------------------ | ----------------- |
| Left candles             | 5                 |
| Right candles            | 5                 |
| Pivot comparison         | High/Low          |
| Minimum pivot separation | 3 candles         |
| Pivot type               | Confirmed fractal |

These are proposed starting defaults, not statistically established optimal values.

**Important:** If a pivot occurs on candle 100 and requires five right-side candles for confirmation, it becomes usable only after candle 105 closes.

The scanner must store both:

- Pivot occurrence timestamp.
- Pivot confirmation timestamp.

That distinction is essential for honest backtesting.

## **2.3 Stage 2: Cluster nearby pivots into horizontal zones**

Suppose we identify the following swing highs:

| Pivot | Price  |
| ----- | ------ |
| P1    | ₹1,020 |
| P2    | ₹1,026 |
| P3    | ₹1,018 |
| P4    | ₹1,023 |
| P5    | ₹1,025 |

These should be treated as a common resistance zone rather than five independent resistance levels.

[配当がない資金でも、最終的には株価上昇に反映されます 株式の利益率＝受け取った配当金 ÷ 投資元本（株価）｜阿部の日経225×日本株](https://images.openai.com/static-rsc-4/haRqrCBUsIS2tZO_r5LTLaJZh-1jp5bpiwp_Uygdz5ttpFR7vjseuXpM4f_yvXiNkY4K-HA5eJyfrI79goF_dSLJdO626C5DzTykQe-hRRmVDYE-lgH8fMt-blwHVJklBgNKi7uNuUxXvlWqOie-C0mhsGiEKt5npWCVnrhDqnI?purpose=inline)

[note.com](https://note.com/dhjghjdfg/n/n9f7ade2ed970?utm_source=chatgpt.com)

I recommend using ATR-normalized clustering.

For a given timeframe:

\\[ D = \max(\alpha \times ATR,\ \beta \times Price) \\]

Where:

- ATR is calculated using the relevant timeframe.
- \\(\alpha\\) is the ATR clustering multiplier.
- \\(\beta\\) is the minimum percentage tolerance.

The initial clustering algorithm can use hierarchical clustering with a bounded maximum cluster width.

Why hierarchical clustering?

- It is easy to explain.
- It is deterministic.
- It works with a relatively small collection of pivot points.
- It allows us to constrain the maximum width of a zone.

I would also retain the individual pivot prices inside each cluster. The final zone should not erase the underlying observations.

## **2.3 Stage 3: Generate support and resistance zones**

Each zone should contain:

| Field                | Description                     |
| -------------------- | ------------------------------- |
| Zone ID              | Unique deterministic identifier |
| Type                 | Support / Resistance            |
| Lower price          | Bottom of zone                  |
| Upper price          | Top of zone                     |
| Representative price | Median pivot price              |
| Touch count          | Independent historical tests    |
| First occurrence     | Earliest contributing pivot     |
| Last occurrence      | Latest contributing pivot       |
| Timeframe            | 5m / 15m / 1h / 1d / 1w         |
| Strength             | Calculated score                |
| Classification       | Major / Minor                   |
| Status               | Active / Broken / Invalidated   |

A zone is not necessarily a single price. It is a price interval.

That is important because market participants rarely defend exactly the same price to the last tick.

## **2.4 Stage 4: Calculate zone strength**

This is where I would differentiate AlgoDesk's scanner from basic support/resistance indicators.

I propose a 100-point scoring system.

| Component                     | Weight  |
| ----------------------------- | ------- |
| Number of independent touches | 20      |
| Successful bounces/rejections | 20      |
| Historical trading volume     | 15      |
| Recency of interaction        | 10      |
| Time since zone formation     | 10      |
| Higher timeframe confluence   | 15      |
| Quality of price reversals    | 10      |
| **Total**                     | **100** |

Each component should have a defined mathematical normalization.

For example, touch count should not increase the score indefinitely. We can use a saturating function:

\\[ S_t = 20 \times \min\left(1,\frac{T}{T\_{\text{target}}}\right) \\]

This prevents a zone with 30 touches from automatically dominating one with 10 genuinely strong touches.

We should also penalize zones that have been repeatedly crossed without meaningful reactions.

The score must be computed using only information available at the screening timestamp.

### **Major versus minor levels**

Proposed initial classification:

| Classification | Strength score |
| -------------- | -------------- |
| Major          | 70–100         |
| Minor          | 40–69          |
| Weak           | Below 40       |

Weak levels may be retained for research but will not be included in the default screening output.

These thresholds should be configurable and eventually calibrated through historical testing.

## **2.5 Stage 5: Detect price interactions with zones**

The scanner should maintain a state machine for every active zone.

[WOI Scanner – US Stock Screener & Technical Rankings | Breakout, Momentum & Compression](https://images.openai.com/static-rsc-4/FookuboNzclOgCFRgPD6louoDM6OxqwPJtcoVXa7jBmPX65mA4RWB4CHrSd7DRA1prNPSs-Fx2PNlT4ynScZRWxn0o7Qo3lUfSowjxy6X9Ozg_mH_kRHknewTbWhhRRkcg43d809TmJyR9Z-panc9wyFFnvDT53MQR91k9C4Bzk?purpose=inline)

[wayofinvestment.com](https://wayofinvestment.com/guides/breakouts-and-fakeouts?utm_source=chatgpt.com)

I propose the following event classifications.

| Event                | Detection condition                                     | Interpretation             |
| -------------------- | ------------------------------------------------------- | -------------------------- |
| Near support         | Price approaches support zone                           | Potential bullish reaction |
| Near resistance      | Price approaches resistance zone                        | Potential breakout         |
| Support bounce       | Support test followed by confirmed upward movement      | Bullish                    |
| Resistance rejection | Resistance test followed by confirmed downward movement | Bearish                    |
| Resistance breakout  | Completed candle closes above resistance                | Bullish                    |
| Support breakdown    | Completed candle closes below support                   | Bearish                    |
| Breakout retest      | Broken resistance is tested from above and holds        | Bullish                    |
| Breakdown retest     | Broken support is tested from below and rejects         | Bearish                    |

### **Breakout confirmation**

A single candle closing marginally above resistance should not automatically be treated as a strong breakout.

I propose:

\\[ Close > Zone\_{upper} + \gamma \times ATR \\]

Where \\(\gamma\\) is a configurable breakout buffer.

Additional confirmation factors:

- Relative volume.
- Candle body size.
- Close position within candle range.
- Higher timeframe resistance above the breakout.
- Whether the breakout occurred during normal market hours.

We should distinguish between:

1. Breakout candidate.
2. Confirmed breakout.
3. Breakout retest.
4. Failed breakout.

This distinction will be useful when we eventually develop strategies using the scanner.

### **Bounce confirmation**

A touch alone is not a bounce.

For example:

- Price enters a support zone.
- A subsequent completed candle closes above the zone.
- The upward movement exceeds a configurable minimum distance.
- Optional bullish candle and volume confirmation.

This allows us to avoid identifying every small fluctuation as a support bounce.

## **2.6 Stage 6: Multi-timeframe analysis**

This is one of the most valuable aspects of the proposed scanner.

A support level on a weekly chart is generally more significant than a minor support level on a five-minute chart, although neither guarantees a reaction.

I recommend analysing each timeframe independently and then combining the results.

## **Multi-timeframe confluence**

**Weekly**

Highest priority

Major structural support and resistance

**Daily**

High priority

Intermediate and major trading levels

**Hourly**

Medium priority

Short-term structural levels

**15-minute**

Intraday trading levels

**5-minute**

Short-term execution levels

Illustrative priority hierarchy, not actual measured strength scores.

For example, imagine a stock has:

- Weekly support at ₹950.
- Daily support at ₹948.
- Hourly support at ₹951.
- Current market price at ₹952.

This is an interesting confluence zone.

The scanner should recognise that these are potentially related levels, not four completely independent signals.

I propose grouping overlapping zones across timeframes into a common confluence zone, while retaining their individual timeframe identities and scores.

This also allows us to distinguish:

- Weekly + daily confluence.
- Daily + hourly confluence.
- Hourly + 15-minute confluence.
- Multiple timeframe confluence.

**Important design decision:** Weekly levels will be calculated from daily candles, and hourly, 15-minute and 5-minute levels will be derived from one-minute candles where available. This avoids maintaining five separate historical datasets.

## **2.7 Stage 7: Market and instrument handling**

Your selection of all markets requires some additional care.

I propose supporting these categories:

| Market category                                       | Treatment                                                                                 |
| ----------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| NSE cash equities                                     | Supported                                                                                 |
| BSE cash equities                                     | Supported                                                                                 |
| NSE stock futures                                     | Supported                                                                                 |
| BSE derivatives, where available in instrument master | Supported subject to data-source capability                                               |
| Stock options                                         | Excluded from default underlying scanner; optional separate contract-level analysis later |
| Index futures                                         | Supported as a separate instrument universe                                               |
| Index options                                         | Optional future extension                                                                 |

There is an important distinction between screening an underlying stock and screening an option contract.

For example, RELIANCE equity may have support at ₹1,400. Its options have different prices, which are affected by volatility, time to expiry and other factors.

We should not apply equity support/resistance prices directly to option premiums.

For stock futures, we need to decide whether to screen each expiry contract separately or screen the underlying cash price and use the result for futures trading.

My recommendation:

- Cash equity: primary structural reference.
- Futures: separate contract-level levels where sufficient historical data exists.
- Options: use the underlying's structural levels, not option premium levels, for the first version.

This prevents contract expiry and roll effects from corrupting the historical levels.

## **4.4 Example configuration**

This is an illustrative configuration. The final schema must be reconciled with the existing screen configuration loader.

```

name = "support_resistance"
plugin = "support_resistance"

[group]
name = "my_custom_universe"

[market]
segments = ["EQ", "FUT"]
exchanges = ["NSE", "BSE"]

[timeframes]
enabled = ["5m", "15m", "1h", "1d", "1w"]

[levels]
include_major = true
include_minor = true
minimum_strength = 40

[pivots]
left_bars = 5
right_bars = 5
minimum_separation = 3

[clustering]
method = "atr_hierarchical"
atr_period = 14
atr_multiplier = 0.5
minimum_price_tolerance_pct = 0.25

[scoring]
touch_weight = 20
reaction_weight = 20
volume_weight = 15
recency_weight = 10
age_weight = 10
timeframe_weight = 15
reversal_quality_weight = 10

[events]
near_support = true
near_resistance = true
support_bounce = true
resistance_rejection = true
breakout = true
breakdown = true
breakout_retest = true
breakdown_retest = true

[output]
format = "table"
save_results = true
```

One important implementation detail: the actual values above are defaults for discussion, not a recommendation to hardcode them in the Python implementation. The shipped parameter registry must remain authoritative.

# **Part 5. Screening output design**

You selected table output. I recommend a single consolidated table rather than five separate tables.

The user should be able to filter by timeframe, event type, strength and instrument category.

## **5.1 Proposed result table**

An illustrative example:

| Stock     | LTP    | Event               | TF  | Level  | Distance | Strength |
| --------- | ------ | ------------------- | --- | ------ | -------- | -------- |
| RELIANCE  | ₹1,450 | Near Support        | 1d  | ₹1,440 | 0.69%    | 86       |
| INFY      | ₹1,720 | Resistance Breakout | 1d  | ₹1,710 | +0.58%   | 91       |
| TCS       | ₹3,180 | Near Resistance     | 1h  | ₹3,200 | -0.63%   | 72       |
| HDFCBANK  | ₹980   | Support Bounce      | 15m | ₹975   | +0.51%   | 83       |
| ICICIBANK | ₹1,350 | Breakout Retest     | 1h  | ₹1,345 | +0.37%   | 88       |

Illustrative data only, not actual market signals.

I would add these columns to the detailed CSV/JSON output:

- Exchange and instrument ID.
- Instrument category (cash/future).
- Event confirmation timestamp.
- Zone lower and upper prices.
- Support/resistance classification.
- Major/minor classification.
- Number of touches.
- Number of successful reactions.
- Strength score.
- ATR.
- Relative volume.
- Weekly/daily/hourly confluence.
- Distance from current price.
- Data source and adjustment basis.
- Signal status and invalidation price.

The CLI table can remain compact, while the saved result contains all details.

## **5.2 Ranking**

A good ranking system is essential.

I suggest the following default ranking:

| Priority | Screening result                   |
| -------- | ---------------------------------- |
| 1        | Strong major-level breakout/retest |
| 2        | Strong major-level bounce          |
| 3        | Near major support/resistance      |
| 4        | Minor-level breakout/retest        |
| 5        | Minor-level bounce                 |
| 6        | Near minor levels                  |

Within each category, rank by strength and multi-timeframe confluence.

Importantly, a strong resistance breakout should not automatically rank higher than a strong support bounce in every trading context. These are different event types and should be filterable independently.


## **7.2 Metrics to calculate**

| Metric                       | Purpose                                                            |
| ---------------------------- | ------------------------------------------------------------------ |
| Bounce success rate          | Percentage of support tests resulting in a qualifying bounce       |
| Rejection success rate       | Percentage of resistance tests resulting in a qualifying rejection |
| Breakout success rate        | Percentage of breakouts that sustain                               |
| Retest success rate          | Percentage of retests that hold                                    |
| Average favourable excursion | Average subsequent favourable price movement                       |
| Average adverse excursion    | Average movement against the anticipated direction                 |
| False breakout rate          | Percentage of breakouts that fail                                  |
| Average reaction time        | Number of candles before a qualifying reaction                     |
| Performance by strength      | Whether stronger levels actually perform better                    |
| Performance by timeframe     | Which timeframe produces the most useful levels                    |

We should measure these across different market regimes, including trending, sideways and volatile periods.

## **7.3 Critical backtesting safeguards**

I would make the following mandatory:

1. No pivot is available before its confirmation candle.
2. No breakout is detected using an incomplete candle.
3. No future candle is used to calculate historical level strength.
4. Corporate actions are applied consistently.
5. Historical instrument membership is respected to avoid survivorship bias.
6. Expired futures are not silently treated as continuous contracts.
7. Missing data and trading holidays are handled correctly.
8. All results are reproducible using the same input data and parameters.

A particularly important point: if we evaluate a historical screen as of 1 January, the algorithm must not use information from February to calculate the January levels.

## **7.4 Proposed validation benchmarks**

I would not set an arbitrary requirement such as 80% bounce success.

Instead, we should establish whether the algorithm provides statistically meaningful improvement over suitable baselines.

For example:

- Randomly selected comparable price levels.
- Simple rolling swing-high/low levels.
- Unclustered pivot levels.
- ATR-clustered levels without strength scoring.

We can then establish whether clustering, touch scoring and timeframe confluence actually improve results.

# **Part 10. Implementation roadmap**

I recommend implementing this in five stages.

1. **Stage 1 – Architecture and interface integration**
   - Verify the actual Screen plugin implementation, if any.
   - Establish the plugin contract.
   - Inspect group configuration and result-writing conventions.
   - Prepare the detailed module design.
   - Register initial parameters.

   Prerequisite
2. **Stage 2 – Core detection engine**
   - Fractal pivot detection.
   - ATR-based clustering.
   - Zone creation.
   - Strength scoring.
   - Major/minor classification.

   Deliverable: tested algorithm operating on supplied OHLCV data.
3. **Stage 3 – Event detection**
   - Near support/resistance.
   - Bounce/rejection.
   - Breakout/breakdown.
   - Breakout/breakdown retests.
   - Multi-timeframe confluence.

   Deliverable: complete event-classification engine.
4. **Stage 4 – AlgoDesk integration**
   - Custom instrument groups.
   - DataService integration.
   - CLI commands.
   - Screening table.
   - Results persistence.
   - Data-readiness reporting.

   Deliverable: usable stock scanner.
5. **Stage 5 – Validation and performance**
   - No-look-ahead tests.
   - Deterministic replay tests.
   - Historical event evaluation.
   - Performance benchmarking.
   - Full `scripts/check.sh`.
   - Documentation and acceptance review.

   Deliverable: tested, documented plugin ready for research use.

# **Part 11. Important risks and design decisions**

There are some areas where I would be particularly cautious.

| Risk                               | My recommendation                                |
| ---------------------------------- | ------------------------------------------------ |
| Too many horizontal lines          | Cluster into bounded zones                       |
| Levels that are repeatedly crossed | Penalise failed reactions                        |
| Repainting pivots                  | Confirmed fractal pivots                         |
| False breakouts                    | Completed candles and ATR buffer                 |
| Too many minor levels              | Separate major/minor classifications             |
| Intraday data limitations          | Explicit provider capability and coverage checks |
| Derivative expiry effects          | Separate contract identities                     |
| Overfitted scoring                 | Out-of-sample validation                         |
| Excessive computation              | Cache intermediate calculations                  |
| Historical bias                    | Strict point-in-time calculations                |

There is also an important distinction between a historically strong level and a currently relevant level.

A level with 15 touches over five years might be less useful than one with four strong reactions over the last six months.

Therefore, strength scoring must incorporate recency and age rather than merely counting touches.

# **Part 12. Final design recommendations**

These are the decisions I would carry into the implementation specification.

| Decision              | Recommendation                                                                           |
| --------------------- | ---------------------------------------------------------------------------------------- |
| Detection algorithm   | Confirmed fractal pivots + ATR clustering                                                |
| Clustering            | Hierarchical, with maximum zone width                                                    |
| Level representation  | Price zones, not just single horizontal prices                                           |
| Strength              | 100-point configurable score                                                             |
| Classification        | Major, Minor, Weak                                                                       |
| Timeframes            | All five, independently calculated                                                       |
| Confluence            | Group overlapping zones across timeframes                                                |
| Events                | Eight classifications                                                                    |
| Data source           | DataService; source selected by instrument/timeframe capability                          |
| Historical adjustment | Split-adjusted technical prices                                                          |
| Instrument universe   | Custom groups                                                                            |
| F&O                   | Separate futures contract analysis; options excluded from initial contract-level scanner |
| Results               | Ranked CLI table + CSV/JSON                                                              |
| Historical testing    | Point-in-time and deterministic                                                          |
| Dependencies          | Minimal, existing approved dependencies preferred                                        |
| Architecture          | Separate Screen plugin                                                                   |