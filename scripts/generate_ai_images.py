"""Generates the image library with Amazon Bedrock and uploads it to S3.

Usage: python scripts/generate_ai_images.py <bucket> <local_folder> [limit]
Images that already exist locally are not generated or paid for again.
"""
import base64
import json
import os
import sys
import time

import boto3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from cdp import content  # noqa: E402

MODEL_ID = "stability.stable-image-core-v1:1"
MODEL_REGION = "us-west-2"
STYLE = (
    "Clean editorial lifestyle photograph for a wellness brand, natural light, warm and welcoming, "
    "high quality, plenty of open space, an empty scene with no people, plain unbranded objects."
)
NEGATIVE = (
    "text, words, letters, logo, watermark, brand names, shoe brand stripes, swoosh, people, person, faces, "
    "close-up of a body, before and after, "
    "medical equipment, hospital, weight scale, tape measure, shirtless, nudity, deformed"
)

SUBJECTS = {
    "outdoor_walk": ["a sunny tree-lined park path at morning", "a coastal walking trail at sunrise", "a neighborhood sidewalk in autumn leaves", "a riverside path at golden hour", "a forest trail with dappled light", "a pair of walking shoes on a park bench", "a hilltop trail with a wide view", "a garden path with spring flowers", "a city greenway with a footbridge", "a lakeside boardwalk in the morning"],
    "home_workout": ["a bright living room with a rolled-out exercise mat", "a pair of light dumbbells beside a sofa", "a resistance band on a wooden floor", "a tablet on a stand next to an exercise mat", "a small sunny home gym corner", "a jump rope on a living room rug", "an exercise ball by a large window", "a water bottle and towel on a home yoga mat", "a tidy garage home workout space", "a kettlebell on a bedroom floor in morning light"],
    "strength": ["a neat rack of dumbbells in a modern gym", "a barbell on a gym floor", "kettlebells in a row on rubber flooring", "a sunlit weight room with no people", "weight plates stacked on a rack", "a squat rack in a clean gym", "chalk and lifting gloves on a bench", "a cable machine in a bright gym", "a bench and dumbbells near a window", "a set of colorful bumper plates"],
    "yoga": ["a yoga mat in a calm sunlit studio", "yoga blocks and a strap on a wooden floor", "a yoga mat on a deck overlooking the water", "a peaceful studio with plants and soft light", "a rolled yoga mat with a folded towel", "a meditation cushion near a window", "a yoga mat on grass at sunrise", "a bright studio with a row of mats", "bamboo and a yoga mat in soft morning light", "a tranquil rooftop with a yoga mat"],
    "group_class": ["an empty bright fitness studio set up for a class", "a row of colorful exercise mats in a studio", "a circle of mats in a community center", "water bottles lined up on a studio floor", "a dance studio with mirrors and warm light", "an outdoor park set up for a fitness class", "a row of stationary bikes in a bright studio", "steps and hand weights arranged for a class", "a welcoming community gym with bunting", "towels and mats laid out for a class"],
    "coaching": ["a notebook with a weekly fitness plan on a desk, no writing visible", "a clipboard and whistle on a gym bench", "a stopwatch and water bottle on a table", "a smartwatch and notebook on a wooden desk", "two chairs in a bright, friendly consultation room", "a tablet and coffee on a table in a gym cafe", "a pair of sneakers beside a planning notebook", "a whiteboard with simple blank boxes in a studio", "a friendly gym reception desk with plants", "a laptop on a table next to a water bottle"],
    "cycling": ["a road bike against a wall on a quiet street", "a bike path through green countryside", "a commuter bike with a basket in a sunny town", "a mountain bike on a forest trail", "a helmet and gloves on a bike seat", "a bike leaning on a fence by a lake", "a row of parked bicycles in a city square", "a cycling path along a beach", "a bike rack in morning light", "a touring bike on a country road"],
    "swimming": ["an empty indoor pool with calm blue water", "lane lines across a bright swimming pool", "goggles and a swim cap by the poolside", "a lake with a small wooden dock at sunrise", "an outdoor pool at golden hour", "a kickboard at the edge of a pool", "gentle ocean waves on a quiet beach", "sunlight rippling on pool water", "a towel on a poolside bench", "a clear mountain lake for swimming"],
    "running": ["a pair of running shoes on a track", "an empty running track at sunrise", "a trail running path through the hills", "running shoes tied on a park bench", "a riverside running path at dawn", "a city bridge running route in morning light", "a beach at low tide with footprints", "lane lines on a red running track", "a water bottle and sneakers by a trail", "a quiet suburban road at sunrise"],
    "daily_routine": ["a morning kitchen table with fruit and a water bottle", "a staircase in a sunlit home", "sneakers by a front door in the morning", "a packed gym bag on a chair", "a healthy breakfast near a window", "a bicycle and backpack by an office door", "a calendar on a wall with blank squares", "a reusable water bottle on an office desk", "a standing desk in a bright room", "a sunny balcony with plants and a stretching mat"],
}


def prompt_for(image_id):
    theme = content.image_theme(image_id)
    subject = SUBJECTS[theme][int(image_id[-2:]) - 1]
    return f"{subject}. {STYLE}"


def generate(client, image_id, seed):
    body = {
        "prompt": prompt_for(image_id),
        "negative_prompt": NEGATIVE,
        "mode": "text-to-image",
        "aspect_ratio": "16:9",
        "output_format": "jpeg",
        "seed": seed,
    }
    response = client.invoke_model(modelId=MODEL_ID, body=json.dumps(body), contentType="application/json")
    data = json.loads(response["body"].read())
    reasons = [reason for reason in data.get("finish_reasons", []) if reason]
    if reasons or not data.get("images"):
        raise RuntimeError(f"no image returned: {reasons}")
    return base64.b64decode(data["images"][0])


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    bucket, folder = sys.argv[1], sys.argv[2]
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else None
    os.makedirs(folder, exist_ok=True)
    bedrock = boto3.client("bedrock-runtime", region_name=MODEL_REGION)
    s3 = boto3.client("s3")

    images = content.all_images()[: limit or None]
    created = uploaded = 0
    failed = []
    for index, image in enumerate(images, start=1):
        path = os.path.join(folder, f"{image['id']}.jpg")
        try:
            if not os.path.exists(path):
                seed_offset = int(os.environ.get("IMAGE_SEED_OFFSET", "0"))  # Change it to redo a rejected image.
                data = generate(bedrock, image["id"], 1000 + index + seed_offset)
                with open(path, "wb") as output:
                    output.write(data)
                created += 1
                time.sleep(1)
            with open(path, "rb") as source:
                s3.put_object(
                    Bucket=bucket,
                    Key=f"{content.IMAGE_PREFIX}{image['id']}.jpg",
                    Body=source.read(),
                    ContentType="image/jpeg",
                )
            uploaded += 1
        except Exception as exc:
            failed.append(image["id"])
            print(f"{image['id']} failed: {type(exc).__name__}: {str(exc)[:220]}")
            if "AccessDenied" in type(exc).__name__ or "AccessDenied" in str(exc):
                break
    print(f"generated {created}, uploaded {uploaded}, failed {len(failed)}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
