
import os
import re
import json
import time
import requests
import html2text


PAGE_SIZE = 20
MAX_RETRIES = 3
OUTPUT_DIR = "知乎收藏导出"


def rename(filename):
    """清理 Windows 文件名中的非法字符。"""
    filename = str(filename)
    filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', filename)
    filename = filename.strip().rstrip('. ')
    return filename[:150] or "未命名内容"


def get_collection_id(url):
    url = url.split('?', 1)[0].rstrip('/')
    return url.split('/')[-1]


def request_json(api_url, cookies, headers, params=None):
    """请求接口，失败时有限重试。"""
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                api_url,
                params=params,
                cookies=cookies,
                headers=headers,
                timeout=30
            )

            try:
                response.raise_for_status()
                data = response.json()
            finally:
                response.close()

            if not isinstance(data, dict):
                raise ValueError("接口返回的 JSON 不是对象")

            return data

        except Exception as e:
            last_error = e
            print(
                f"请求失败（{attempt}/{MAX_RETRIES}）："
                f"{type(e).__name__}: {e}"
            )

            if attempt < MAX_RETRIES:
                time.sleep(attempt * 2)

    raise RuntimeError(f"请求连续失败：{last_error}")


def get_collection_api_url(url):
    collection_id = get_collection_id(url)

    if not collection_id.isdigit():
        raise ValueError("收藏夹 URL 中没有有效的数字 ID")

    return (
        "https://www.zhihu.com/api/v4/collections/"
        f"{collection_id}/items"
    )


def get_answer_count(url, cookies, headers, params):
    """
    尝试读取接口分页总数。
    如果无法确认总数，返回 None，改用逐页请求直到没有数据。
    """
    api_url = get_collection_api_url(url)
    data = request_json(api_url, cookies, headers, params)

    paging = data.get("paging") or {}

    # paging.totals 是接口分页总数的常见位置。
    # 不把当前页 data 的长度当作收藏夹总数。
    totals = paging.get("totals")

    if totals is not None:
        try:
            return int(totals)
        except (TypeError, ValueError):
            pass

    # 兼容部分接口结构
    for key in ("answerCount", "total"):
        if data.get(key) is not None:
            try:
                return int(data[key])
            except (TypeError, ValueError):
                pass

    print("警告：接口未提供可识别的总数，将逐页读取。")
    return None


def get_page_json(url, cookies, headers, params):
    api_url = get_collection_api_url(url)
    data = request_json(api_url, cookies, headers, params)

    items = data.get("data")

    if not isinstance(items, list):
        raise ValueError(
            "接口没有返回 data 列表。"
            f"返回字段：{list(data.keys())}"
        )

    return items


def extract_html(content):
    """尽可能提取回答、文章和想法中的 HTML 正文。"""
    body = content.get("content", "")

    if isinstance(body, str):
        return body

    if isinstance(body, list):
        parts = []

        for block in body:
            if not isinstance(block, dict):
                continue

            block_content = block.get("content", "")

            if isinstance(block_content, str):
                parts.append(block_content)
            elif isinstance(block_content, list):
                parts.append(
                    extract_html({"content": block_content})
                )

        return "\n".join(parts)

    return ""


def get_answer_content(answer):
    content = answer.get("content") or {}

    if not isinstance(content, dict):
        raise ValueError("content 字段不是对象")

    # 回答的标题通常位于 question.title
    question = content.get("question") or {}
    title = None

    if isinstance(question, dict):
        title = question.get("title")

    # 文章等内容可能直接具有 title
    if not title:
        title = content.get("title")

    body = content.get("content", "")

    # 兼容想法或其他块结构
    if not title and isinstance(body, list):
        for block in body:
            if isinstance(block, dict) and block.get("title"):
                title = block["title"]
                break

    # 备用标题
    if not title:
        title = content.get("excerpt") or content.get("name")

    if not title:
        content_id = (
            content.get("id")
            or answer.get("id")
            or "unknown"
        )
        title = f"未识别内容_{content_id}"

    title = rename(title)

    html_content = extract_html(content)

    if not html_content and content.get("type") == "video":
        html_content = "请打开原始链接观看视频。"

    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.body_width = 0

    try:
        md_text = converter.handle(html_content)
    except Exception as e:
        print(f"正文转换失败：{e}")
        md_text = html_content

    author = content.get("author") or {}

    if isinstance(author, dict):
        author_name = author.get("name", "未知作者")
    else:
        author_name = "未知作者"

    answer_url = content.get("url") or ""

    if not answer_url:
        content_id = content.get("id") or answer.get("id")
        if content_id:
            answer_url = (
                "https://www.zhihu.com/answer/"
                f"{content_id}"
            )

    return {
        "title": title,
        "md_text": md_text,
        "url": answer_url,
        "author": author_name,
    }


def save_answer(number, answer):
    item = get_answer_content(answer)

    title = rename(item["title"])
    filename = os.path.join(
        OUTPUT_DIR,
        f"{number}. {title}.md"
    )

    if item["url"]:
        header = (
            f"> Author: [{item['author']}]"
            f"({item['url']})\n\n"
        )
    else:
        header = f"> Author: {item['author']}\n\n"

    with open(filename, "w", encoding="utf-8") as file:
        file.write(header)
        file.write(item["md_text"])

    print(f"{number}. {title}")
    if item["url"]:
        print(item["url"])


def re_connect(start, end, url, params, cookies, headers):
    """
    start 是从 0 开始的偏移量。
    end 是已知总数；如果 end 为 None，则持续读取到空页。
    """
    limit = int(params.get("limit", PAGE_SIZE))
    offset = start
    consecutive_failures = 0

    while end is None or offset < end:
        page = offset // limit + 1

        print(f"\n第 {page} 页")
        print(f"当前起始位置：{offset}")
        time.sleep(1)

        page_params = params.copy()
        page_params["offset"] = offset
        page_params["limit"] = limit

        try:
            answers = get_page_json(
                url, cookies, headers, page_params
            )
            consecutive_failures = 0

        except Exception as e:
            consecutive_failures += 1

            print(
                f"第 {page} 页请求失败："
                f"{type(e).__name__}: {e}"
            )

            if consecutive_failures >= MAX_RETRIES:
                print(
                    f"连续失败 {MAX_RETRIES} 次，停止导出。"
                    f"下次可尝试从 offset={offset} 继续。"
                )
                return

            time.sleep(consecutive_failures * 3)
            continue

        if not answers:
            print("接口返回空页，导出结束。")
            break

        for i, answer in enumerate(answers):
            number = offset + i + 1

            try:
                save_answer(number, answer)
            except Exception as e:
                print(
                    f"第 {number} 条导出失败："
                    f"{type(e).__name__}: {e}"
                )

                content = answer.get("content") or {}
                if isinstance(content, dict):
                    print("内容字段：", list(content.keys()))

                continue

        # 必须按实际返回条数推进，而不是固定增加 20。
        offset += len(answers)

        print(f"本页处理完毕，已遍历 {offset} 条。")

        # 不根据不足 20 条判断结束。
        # 下一次请求若为空页，才会结束。
        if len(answers) < limit:
            print("本页返回数量不足一页，继续请求下一页确认。")

    print("\n导出流程结束。")


def main():
    cookie_file = "Cookies.json"

    if not os.path.exists(cookie_file):
        print(f"找不到 {cookie_file}，请确认文件位置。")
        return

    try:
        with open(cookie_file, "r", encoding="utf-8") as file:
            cookies = json.load(file)
    except Exception as e:
        print(f"读取 Cookies.json 失败：{e}")
        return

    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "x-requested-with": "fetch",
    }

    url = input("请输入收藏夹 URL：").strip()

    if not url:
        print("URL 不能为空。")
        return

    params = {
        "offset": 0,
        "limit": PAGE_SIZE,
    }

    try:
        total = get_answer_count(
            url, cookies, headers, params
        )
    except Exception as e:
        print(f"获取收藏夹总数失败：{e}")
        print("将尝试逐页读取，直到接口返回空页。")
        total = None

    if total is None:
        print("收藏夹总数未知，将通过分页确定结束位置。")
    else:
        print(f"收藏夹接口返回总数：{total}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"Markdown 文件将保存到：{os.path.abspath(OUTPUT_DIR)}")

    # 从 0 开始完整导出。
    # 如果确认前 116 条已完整导出，可改为 116。
    start = 0

    re_connect(
        start,
        total,
        url,
        params,
        cookies,
        headers,
    )


if __name__ == "__main__":
    main()
