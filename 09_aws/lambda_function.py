import json
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

import boto3

PARAMETER_NAME = "/home-power/nature-remo/access-token"
TABLE_NAME = "PowerMeasurementState"
METER_ID = "home"
NAMESPACE = "HomePower"
API_URL = "https://api.nature.global/1/appliances"

# 積算電力量の単位を表す値と、kWhへ換算するときに掛ける値
UNIT_MAP = {
    "0": Decimal("1"),
    "1": Decimal("0.1"),
    "2": Decimal("0.01"),
    "3": Decimal("0.001"),
    "4": Decimal("0.0001"),
    "10": Decimal("10"),
    "11": Decimal("100"),
    "12": Decimal("1000"),
    "13": Decimal("10000"),
}

ssm = boto3.client("ssm")
cloudwatch = boto3.client("cloudwatch")
table = boto3.resource("dynamodb").Table(TABLE_NAME)


def get_access_token():
    # Parameter Storeからアクセストークンを読み取る
    response = ssm.get_parameter(
        Name=PARAMETER_NAME, WithDecryption=True
    )
    return response["Parameter"]["Value"]


def fetch_appliances(token):
    # Nature Remo Cloud APIから機器の一覧を取得する
    request = urllib.request.Request(
        API_URL, headers={"Authorization": "Bearer " + token}
    )
    with urllib.request.urlopen(request, timeout=10) as res:
        return json.loads(res.read())


def extract_power_values(appliances):
    # スマートメーターの瞬時電力(W)と積算電力量(kWh)を取り出す
    for appliance in appliances:
        meter = appliance.get("smart_meter")
        if not meter:
            continue

        props = {}
        for item in meter.get("echonetlite_properties", []):
            props[item.get("name")] = item.get("val")

        power = props.get("measured_instantaneous")
        energy = props.get(
            "normal_direction_cumulative_electric_energy"
        )
        unit = props.get("cumulative_electric_energy_unit")
        coefficient = props.get("coefficient", "1")

        if power is None or energy is None:
            raise ValueError("電力データが取得できません")
        if unit not in UNIT_MAP:
            raise ValueError("想定外の単位です: " + str(unit))

        cumulative_kwh = (
            Decimal(energy) * Decimal(coefficient)
            * UNIT_MAP[unit]
        )
        return int(power), cumulative_kwh

    raise ValueError("スマートメーターのデータが見つかりません")


def lambda_handler(event, context):
    # 1. アクセストークンを読み取り、電力データを取得する
    token = get_access_token()
    power, cumulative_kwh = extract_power_values(
        fetch_appliances(token)
    )
    now = datetime.now(timezone.utc)

    metrics = [
        {"MetricName": "InstantaneousPower", "Timestamp": now, 
         "Value": power, "Unit": "None"},
        {"MetricName": "CumulativeEnergy", "Timestamp": now,
         "Value": float(cumulative_kwh), "Unit": "None"},
    ]

    # 2. 前回の積算電力量との差から、使用電力量を計算する
    key = {"meter_id": METER_ID}
    previous = table.get_item(Key=key).get("Item")
    usage_kwh = None
    elapsed_minutes = None
    if previous and "cumulative_kwh" in previous:
        prev_time = datetime.fromisoformat(
            previous["measured_at"]
        )
        elapsed = now - prev_time
        elapsed_minutes = round(elapsed.total_seconds() / 60)
        usage_kwh = cumulative_kwh - previous["cumulative_kwh"]
        if usage_kwh >= 0:
            # 使用電力量は、前回の取得日時の値として記録する
            metrics.append({"MetricName": "EnergyUsage",
                            "Timestamp": prev_time,
                            "Value": float(usage_kwh),
                            "Unit": "None"})
        else:
            print("前回より小さいため記録しません")
            usage_kwh = None

    # 3. CloudWatchへ電力データを送る
    cloudwatch.put_metric_data(
        Namespace=NAMESPACE, MetricData=metrics
    )

    # 4. 送信が終わった後に、今回の積算電力量を保存する
    table.put_item(Item={
        "meter_id": METER_ID,
        "cumulative_kwh": cumulative_kwh,
        "measured_at": now.isoformat(),
    })

    result = {
        "power_w": power,
        "cumulative_kwh": str(cumulative_kwh),
        "usage_kwh": (
            None if usage_kwh is None else str(usage_kwh)
        ),
        "elapsed_minutes": elapsed_minutes,
    }
    print(json.dumps(result, ensure_ascii=False))
    return result
