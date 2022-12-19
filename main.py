import objection_engine
from cohost.models.user import User
from cohost.models.block import MarkdownBlock
from cohost.network import fetch

import re
import configparser
import logging, logging.config
from datetime import datetime
import time
import requests
from shutil import move
import os
import glob
import json
from markdown import markdown
from bs4 import BeautifulSoup

# unused because api appears to return comments in time order, but keeping the code just in case
#def get_comment_unix_timestamp(comment):
#    date_time = datetime.fromisoformat(comment[1].get("comment").get("postedAtISO"))
#    return int(time.mktime(date_time.timetuple()))

def reply_comment(comment_id, message):
    switch_dict = {"project_id": project.projectId}
    requests.post("https://cohost.org/rc/project/switch", data=json.dumps(switch_dict), cookies={"connect.sid": user.cookie}, headers={"Content-Type": "application/json"})
    data_dict = {"inReplyToCommentId": comment_id, "postId": int(config["acecourtbot"]["masterpostid"]), "body": message}
    requests.post("https://cohost.org/api/v1/comments/", data=json.dumps(data_dict), cookies={"connect.sid": user.cookie}, headers={"Content-Type": "application/json"})

def strip_md_html(content: str):
    html = markdown(content)
    html = re.sub(r'<pre>(.*?)</pre>', ' ', html)
    html = re.sub(r'<code>(.*?)</code>', ' ', html)

    soup = BeautifulSoup(html, "html.parser")
    text = ''.join(soup.findAll(text=True))

    return text

def create_objection_comments_from_post(post):
    comments = []
    if post["transparentShareOfPostId"] == None:
            evidence = None
            evidence_path = None
            for block in post["blocks"]:
                if block["type"] == "attachment":
                    evidence = block["attachment"]["fileURL"]
                    evidence_path = "evidence_"+ str(post["postId"]) + ".img.tmp"
                    with open(evidence_path, "w+b") as img:
                        rq = requests.get(evidence)
                        if rq.status_code == 200:
                            img.write(rq.content)
                        else:
                            log.warning("Received status " + str(rq.status_code) + " when retrieving image " + evidence)
                        img.close()
                    break # only first image should be evidence!
            id = post["postingProject"]["projectId"]
            handle = "@" + post["postingProject"]["handle"]
            if len(post["headline"]) > 0:
                head_comment = objection_engine.comment.Comment(id, handle, post["headline"])
                comments.append(head_comment)
            if len(post["plainTextBody"]) > 0:
                for c in strip_md_html(post["plainTextBody"]).split("\n"):
                    if len(c) > 0:
                        body_comment = objection_engine.comment.Comment(id, handle, c)
                        if evidence != None:
                            body_comment.evidence_path = evidence_path
                        comments.append(body_comment)
            elif evidence != None:
                # attach evidence to headline if body not present
                if len(comments) > 0:
                    comments[0].evidence_path = evidence_path
                else:
                    # if post only contains image
                    img_comment = objection_engine.comment.Comment(id, handle, "[image]", evidence_path)
                    comments.append(img_comment)
    return comments

def create_objection_comments_from_sharetree(sharetree):
    comments = []
    for share in sharetree:
            for c in create_objection_comments_from_post(share):
                comments.append(c)
    return comments

def find_last_unique_post_id(post):
    if post["transparentShareOfPostId"] == None:
        return post["postId"]
    tree = post["shareTree"].copy()
    tree.reverse()
    for p in tree:
        if p["transparentShareOfPostId"] == None:
            return p["postId"]
    raise "Every post in the share tree is transparently shared???"


def render_video_from_post(post, requester, comment_id):
    comments = []
    id = str(find_last_unique_post_id(post))
    for c in create_objection_comments_from_sharetree(post["shareTree"]):
        comments.append(c)
    for c in create_objection_comments_from_post(post):
        comments.append(c)
    objection_engine.render_comment_list(comments, id + ".mp4")
    move(os.path.abspath(id + ".mp4"), os.path.abspath(config["acecourtbot"]["outpath"] + id + ".mp4"))

    evidence_files = glob.glob("evidence*.img.tmp")
    for f in evidence_files:
        os.remove(f)

    headline = comments[0].text_content[:138].strip()
    blocks = [
        MarkdownBlock("[Original thread here](" + post["singlePostPageUrl"] + ")"),
        MarkdownBlock("Requested by @" + requester), 
        MarkdownBlock(config["acecourtbot"]["urlprefix"] + id + ".mp4")
    ]
    link = project.post(headline="\"" + headline + "\"", blocks=blocks, tags=["acecourtbot", "bot"])
    reply_comment(comment_id, "Your video has been successfully rendered and posted! Check it out [here!](" + link.url + ")")
    

def fetch_comments():
    log.info("Polling comments...")
    comments = fetch("GET", "/project_post/" + str(config["acecourtbot"]["masterpostid"]) + "/comments", data={}, cookies={"connect.sid": user.cookie}).get("comments")
    #sorted(comments.items(), key = get_comment_unix_timestamp)
    for c in comments:
        comment = comments.get(c).get("comment")
        date_time = datetime.fromisoformat(comment.get("postedAtISO"))
        post_unix_timestamp = int(time.mktime(date_time.timetuple()))
        last_unix_timestamp = int(config["acecourtbot"]["LastCommentProcessedTimestamp"])
        if post_unix_timestamp > last_unix_timestamp and not comment["deleted"] and comment["inReplyTo"] == None:
                log.debug("Processing timestamp " + str(post_unix_timestamp) + ", last is " + str(last_unix_timestamp))
                comment_poster = comments.get(c).get("poster").get("handle")
                body = comment.get("body")
                command = body.split(" ")
                if command[0] == "render":
                    match = re.search("https://cohost.org/.+/post/([0-9]+)\S+", command[1])
                    if type(match) == re.Match:
                        postId = match.group(1)
                        post = fetch("GET", "/project_post/" + postId, data={}, cookies={"connect.sid": user.cookie})
                        try:
                            # the nightmare below exists because the share tree isnt exposed when fetching a single post
                            # you can only get shares on a post by iterating through the entire account until you find the post again
                            # this sucks
                            for l in post.get("_links"):
                                if l["rel"] == "projectPosts":
                                    endpoint = str(l["href"]).partition("/api/v1")[2]
                                    page = 0
                                    exitLoop = False
                                    log.info("Looking for id " + str(postId))
                                    while not exitLoop:
                                        log.info("Fetching page " + str(page))
                                        # idk how to increase number of posts per page and indeed dont know if i even can
                                        # but i would like to make less pagination calls so it would be good to find out
                                        posts = fetch("GET", endpoint, data={"page": str(page)}, cookies={"connect.sid": user.cookie})
                                        if posts.get("nItems") == "0":
                                            break
                                        else:
                                            found = False
                                            for p in posts.get("items"):
                                                if(str(p["postId"]) == str(postId)):
                                                    log.info("Post found!")
                                                    unique_id = find_last_unique_post_id(p)
                                                    log.info("Passed id is " + str(postId) + ", unique id is " + str(unique_id))
                                                    if not os.path.exists(config["acecourtbot"]["outpath"] + str(unique_id) + ".mp4"):
                                                        log.info("Video does not exist, rendering...")
                                                        render_video_from_post(p, comment_poster, comment["commentId"])
                                                        log.info("Finished rendering")
                                                    else:
                                                        log.info("Video already exists, skipping...")
                                                        reply_comment(comment["commentId"], "That thread has already been rendered! To avoid replacing the previous video, your video will not be rendered. The previous video can be found at [this link](" + config["acecourtbot"]["urlprefix"] + str(unique_id) + ".mp4).")
                                                    found = True
                                                    exitLoop = True
                                                    break
                                            if not found:
                                                page += 1
                                                time.sleep(2)
                                    break
                        except Exception as e:
                            log.exception("Error occurred processing post " + str(postId) + ", comment " + comment["commentId"])
                            reply_comment(comment["commentId"], "An error occurred while processing your video! Try again later, and if the problem persists, that means @glotch needs to check his logs!!")
                    else:
                        reply_comment(comment["commentId"], "Post link not detected. Please make sure to provide a full and valid Cohost post link.")
                else:
                    reply_comment(comment["commentId"], "Invalid command! Please refer to the contents of the master post for a list of valid commands and how to use them.")
                config["acecourtbot"]["LastCommentProcessedTimestamp"] = str(post_unix_timestamp)
                with open("./config.ini", "w") as configfile:
                    config.write(configfile)
    log.info("Comment processing finished, waiting 60 sec...")
    time.sleep(60)

if __name__ == "__main__":
    config = configparser.ConfigParser()
    config.read("./config.ini")
    if not config.has_section("acecourtbot"):
        config.add_section("acecourtbot")

    if not config.has_option("acecourtbot", "LastCommentProcessedTimestamp"):
        config["acecourtbot"]["LastCommentProcessedTimestamp"] = "0"

    user = User.login(config["acecourtbot"]["cohostuser"], config["acecourtbot"]["cohostpass"])

    project = user.getProject(config["acecourtbot"]["botprojecthandle"])

    logging.config.dictConfig({"disable_existing_loggers": True, "version": 1})
    log = logging.getLogger("acecourtbot")
    fh = logging.FileHandler(config["acecourtbot"]["logpath"] + "acb.log")
    sh = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    log.addHandler(fh)
    log.addHandler(sh)
    log.setLevel(config["acecourtbot"]["loglevel"])

    while True:
        fetch_comments()