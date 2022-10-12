#!/usr/bin/env python3

import json
import shutil
import sys
from collections import abc
from configparser import ConfigParser
from datetime import datetime
from pathlib import Path

from jinja2 import Template
import psycopg2
import pygal
from pygal.style import DefaultStyle as pygalStyle


def config(section):
    parser = ConfigParser()
    parser.read("bareport.conf")
    db = {}
    for param in parser.items(section):
        db[param[0]] = param[1]
    return db


def run(query, database, instance):
    conn = None
    try:
        params = config(instance)
        params["database"] = database
        conn = psycopg2.connect(**params)
        cur = conn.cursor()
        cur.execute(query)
        result = cur.fetchall()
        cur.close()
        return result
    except (Exception, psycopg2.DatabaseError) as error:
        print(
            "Database error on instance %s, database %s: %s"
            % (instance, database, error),
            file=sys.stderr,
        )
    finally:
        if conn is not None:
            conn.close()


def get_client_data(client, instance):
    date = datetime.now().strftime("%Y-%m-%d")
    jobs = run(
        "SELECT name, jobbytes FROM public.job WHERE type='B' AND (jobstatus='T' OR jobstatus='W')",
        client,
        instance,
    )
    if not jobs:
        return
    total, hosts, filesets = {date: 0}, {}, {}
    for job in jobs:
        name = job[0]
        host = name.split("_")[0]
        size = job[1]
        if not host in hosts:
            hosts[host] = {"total": {date: 0}, "filesets": {}}
        try:
            fileset = name.split("_")[1]
            if not fileset in hosts[host]["filesets"]:
                hosts[host]["filesets"][fileset] = {"total": {date: 0}}
            hosts[host]["filesets"][fileset]["total"][date] += size
        except IndexError:
            pass  # not a fileset backup job
        hosts[host]["total"][date] += size
        total[date] += size
    return {"hosts": hosts, "total": total, "instances": [instance]}


def dict_merge(dct, merge_dct):
    """See https://gist.github.com/angstwad/bf22d1822c38a92ec0a9"""
    for k, v in merge_dct.items():
        if (
            k in dct
            and isinstance(dct[k], dict)
            and isinstance(merge_dct[k], abc.Mapping)
        ):
            dict_merge(dct[k], merge_dct[k])
        elif k in dct and isinstance(dct[k], int) and isinstance(merge_dct[k], int):
            dct[k] += merge_dct[k]
        elif k in dct and isinstance(dct[k], list) and isinstance(merge_dct[k], list):
            dct[k] = list(set(dct[k] + merge_dct[k]))
            dct[k].sort()
        else:
            dct[k] = merge_dct[k]


def get_clients_data(instance):
    clients, nodata_clients = {}, []
    json_path_combined = Path(config("web")["path"], "json")
    json_path = Path(json_path_combined, instance)
    json_path.mkdir(parents=True, exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d")
    # if client list is specified in config, use it
    # otherwise we consider that each database corresponds to a client
    if "names" in config("clients"):
        client_names = config("clients")["names"].split(",")
    else:
        client_names = [
            client[0]
            for client in run(
                "SELECT datname FROM pg_database WHERE datistemplate = false",
                "postgres",
                instance,
            )
        ]
    for client in client_names:
        if client == "postgres":
            continue
        paths_previous = [
            path
            for path in Path(json_path).glob("%s.json-*" % client)
            if str(path)[-10:] != date
        ]
        path_new = Path(json_path, "%s.json-%s" % (client, date))
        path_combined = Path(json_path_combined, "%s.json" % client)
        previous = {}
        # read data from previous days
        for path in paths_previous:
            try:
                with path.open("r") as file:
                    dict_merge(previous, json.loads(file.read()))
            except (FileNotFoundError, json.decoder.JSONDecodeError):
                print("Error reading %s." % path, file=sys.stderr)
                pass
            except AttributeError:
                pass
        # get today's data and save it
        new = get_client_data(client, instance)
        with path_new.open("w") as file:
            file.writelines(json.dumps(new))
        # merge all data and save it
        if not new and previous:
            new = previous
        elif not new:
            print("No data for %s, ignoring." % client, file=sys.stderr)
            nodata_clients.append(f"{client} ({instance})")
            continue
        dict_merge(new, previous)
        with path_combined.open("w") as file:
            file.writelines(json.dumps(new))
        clients[client] = new
    return clients, nodata_clients


def render_timeline(name, directory, data, xlink_dir=None):
    web_path = config("web")["path"]
    timelines_path_svg = Path(web_path, "svg", "timelines", directory)
    timelines_path_html = Path(web_path, "html", "timelines", directory)
    timelines_path_svg.mkdir(parents=True, exist_ok=True)
    timelines_path_html.mkdir(parents=True, exist_ok=True)
    lines, dates = [], []
    for item, item_data in data.items():
        dates += [key for key in item_data["total"]]
    dates = sorted(set(dates))
    for item, item_data in data.items():
        values = []
        for date in dates:
            value = {"label": date}
            if date in item_data["total"]:
                value["value"] = item_data["total"][date] / 2 ** 30
            if xlink_dir:
                value["xlink"] = "../%s/%s.svg" % (xlink_dir, item)
            values.append(value)
        lines.append([item, values])
    line_chart = pygal.Line(
        allow_interruptions=True,
        fill=False,
        interpolate="cubic",
        show_minor_x_labels=False,
        show_only_major_dots=True,
        style=pygalStyle,
        x_label_rotation=45,
        x_labels=dates,
        x_labels_major_every=5,
        x_title="Date",
        y_title="Volume (in Gio)",
    )
    line_chart.title = "%s volume usage evolution" % name
    for line in lines:
        line_chart.add(line[0], line[1])
    line_chart.render_to_file(str(Path(timelines_path_svg, "%s.svg" % name)))
    with Path(timelines_path_html, "%s.html" % name).open("w") as file:
        file.writelines(line_chart.render_table(style=True, transpose=True))


def render_timelines(clients):
    render_timeline("all_clients", "clients", clients, xlink_dir="clients")
    for count, (client, client_data) in enumerate(clients.items(), start=1):
        print(count, end="… ", file=sys.stderr, flush=True)
        render_timeline(
            "%s_total" % client, "clients", {client: client_data}, xlink_dir="clients"
        )
        render_timeline(client, "clients", client_data["hosts"], xlink_dir="hosts")
        for host, host_data in client_data["hosts"].items():
            render_timeline(host, "hosts", host_data["filesets"])
    print("", file=sys.stderr)


def render_treemaps(clients):
    web_path = config("web")["path"]
    treemaps_path_svg = Path(web_path, "svg", "treemaps", "clients")
    treemaps_path_html = Path(web_path, "html", "reports", "clients")
    treemaps_path_svg.mkdir(parents=True, exist_ok=True)
    treemaps_path_html.mkdir(parents=True, exist_ok=True)
    for count, (client, client_data) in enumerate(clients.items(), start=1):
        print(count, end="… ", file=sys.stderr, flush=True)
        treemap = pygal.Treemap(style=pygalStyle)
        treemap.title = "%s volume usage repartition" % client
        last_client_backup_date = datetime.strptime(
            max(client_data["total"]), "%Y-%m-%d"
        )
        for host, host_data in client_data["hosts"].items():
            values = []
            last_host_backup_date = datetime.strptime(
                max(host_data["total"]), "%Y-%m-%d"
            )
            if last_host_backup_date < last_client_backup_date:
                print(
                    "Skipping host %s in treemap: last backup (%s) < last client backup (%s)"
                    % (host, last_host_backup_date, last_client_backup_date)
                )
                continue
            for fileset, fileset_data in host_data["filesets"].items():
                last_fileset_backup_date = datetime.strptime(
                    max(fileset_data["total"]), "%Y-%m-%d"
                )
                if last_fileset_backup_date < last_host_backup_date:
                    print(
                        "Skipping fileset %s in treemap: last backup (%s) < last host backup (%s)"
                        % (host, last_host_backup_date, last_host_backup_date)
                    )
                    continue
                volume = fileset_data["total"][max(fileset_data["total"])]
                value = {
                    "value": volume / 2 ** 30,
                    "label": fileset,
                    "xlink": "../../timelines/hosts/%s.svg" % host,
                }
                values.append(value)
            treemap.add(host, values)
        treemap.render_to_file(str(Path(treemaps_path_svg, "%s.svg" % client)))
        with Path(treemaps_path_html, "%s.html" % client).open("w") as file:
            file.writelines(
                treemap.render_table(style=True, total=True, transpose=True)
            )
    print("", file=sys.stderr)


def render_html(clients, template_name):
    index_path = Path(config("web")["path"])
    index_path.mkdir(parents=True, exist_ok=True)
    with Path("%s.html.j2" % template_name).open("r") as file:
        template = Template(file.read())
    page = template.render(clients=clients)
    with Path(index_path, "%s.html" % template_name).open("w") as file:
        file.writelines(page)


def render_clients_html(clients):
    pages_path = Path(config("web")["path"], "clients")
    pages_path.mkdir(parents=True, exist_ok=True)
    for client, client_data in clients.items():
        with Path("clients.html.j2").open("r") as file:
            template = Template(file.read())
        page = template.render(
            client=client, client_data=client_data, hosts=client_data["hosts"]
        )
        with Path(pages_path, "%s.html" % client).open("w") as file:
            file.writelines(page)


def copy_static_files():
    css_path = Path(config("web")["path"])
    css_path.mkdir(parents=True, exist_ok=True)
    for filename in ["styles.css", "logo.png"]:
        shutil.copy(str(Path(filename)), str(Path(css_path, filename)))


if __name__ == "__main__":
    clients, nodata_clients = {}, []
    print(
        "bareport - %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"), file=sys.stderr
    )
    print("Step 1/5: collecting data", file=sys.stderr)
    for instance in config("instances")["names"].split(","):
        clients_new, nodata_clients_new = get_clients_data(instance)
        dict_merge(clients, clients_new)
        nodata_clients += nodata_clients_new
    clients = dict(sorted(clients.items(), key=lambda kv: kv[0]))
    print(
        "Step 2/5: rendering timelines for %d clients" % len(clients), file=sys.stderr
    )
    render_timelines(clients)
    print("Step 3/5: rendering treemaps for %d clients" % len(clients), file=sys.stderr)
    render_treemaps(clients)
    print("Step 4/5: rendering html", file=sys.stderr)
    render_html(clients, "index")
    render_html(clients, "all_clients")
    render_html(nodata_clients, "nodata")
    render_clients_html(clients)
    print("Step 5/5: copying static files", file=sys.stderr)
    copy_static_files()
