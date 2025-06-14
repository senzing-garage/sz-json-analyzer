#! /usr/bin/env python3

import sys
import os
import argparse
import csv
import json
import time
from datetime import datetime
import signal
import io
import subprocess
from contextlib import suppress

try:
    import prettytable
except (ImportError, ModuleNotFoundError) as err:
    input(err)
    prettytable = None


def get_config_data(config_file_name):
    # ignore cached file IO errors as just for convenience
    config_message = (
        "Access to Senzing instance needed to get current configuration data!"
    )
    config_data = None
    try:
        from senzing import G2Config, G2ConfigMgr

        iniParams = os.getenv("SENZING_ENGINE_CONFIGURATION_JSON")
        # if not iniParams:

        g2ConfigMgr = G2ConfigMgr()
        g2ConfigMgr.init("pyG2ConfigMgr", iniParams, False)
        defaultConfigID = bytearray()
        g2ConfigMgr.getDefaultConfigID(defaultConfigID)
        defaultConfigDoc = bytearray()
        g2ConfigMgr.getConfig(defaultConfigID, defaultConfigDoc)
        cfgData = json.loads(defaultConfigDoc.decode())
        g2ConfigMgr.destroy()
        config_data = {"G2_CONFIG": {}}
        config_data["G2_CONFIG"]["CFG_DSRC"] = cfgData["G2_CONFIG"]["CFG_DSRC"]
        config_data["G2_CONFIG"]["CFG_ATTR"] = cfgData["G2_CONFIG"]["CFG_ATTR"]
        config_data["G2_CONFIG"]["CFG_FTYPE"] = cfgData["G2_CONFIG"]["CFG_FTYPE"]
        config_message = "Using current configuration data"

        with suppress(Exception):
            with open(config_file_name, "w") as f:
                json.dump(config_data, f, indent=4)

    except Exception as err:
        config_message = err
        if os.path.exists(config_file_name):
            with suppress(Exception):
                with open(config_file_name, "r") as f:
                    config_data = json.loads(f.read())
                    config_message = "Using previously cached configuration data"

    return config_data, config_message


# =========================
class SzJsonAnalyzer:

    def __init__(self, config_data):

        self.record_count = 0

        self.data_source_lookup = {}
        for record in config_data["G2_CONFIG"]["CFG_DSRC"]:
            self.data_source_lookup[record["DSRC_CODE"]] = record

        self.feature_lookup = {}
        for record in config_data["G2_CONFIG"]["CFG_FTYPE"]:
            self.feature_lookup[record["FTYPE_CODE"]] = record

        self.attribute_lookup = {}
        self.required_attributes = {}
        self.label_to_attribute = {}
        self.feature_order = {}
        for record in config_data["G2_CONFIG"]["CFG_ATTR"]:
            self.attribute_lookup[record["ATTR_CODE"]] = record
            ftype_code = (
                record["FTYPE_CODE"] if record["FTYPE_CODE"] else record["ATTR_CODE"]
            )
            if ftype_code not in self.required_attributes:
                self.required_attributes[ftype_code] = []
            if record["FELEM_REQ"] != "No":
                self.required_attributes[ftype_code].append(record)
            if record["FELEM_CODE"] == "USAGE_TYPE":
                self.label_to_attribute[ftype_code] = record["ATTR_CODE"]
            if ftype_code not in self.feature_order:
                self.feature_order[ftype_code] = record["ATTR_ID"]
            elif self.feature_order[ftype_code] < record["ATTR_ID"]:
                self.feature_order[ftype_code] = record["ATTR_ID"]

        self.feature_order["RECORD_TYPE"] = (
            1004  # hack until 4.0 to mover record_type higher
        )

        self.max_values_per_attr = 1000000
        self.mapped_attribute = {}
        self.attribute_stats = {}
        self.unmapped_stats = {}
        self.feature_stats = {}
        self.message_stats = {"ERROR": {}, "WARNING": {}, "INFO": {}}

    def register_attribute(self, attr_name):
        attr_data = {}
        if attr_name in self.attribute_lookup:
            attr_data = self.attribute_lookup[attr_name]
        elif "_" in attr_name:
            possible_label = attr_name[0 : attr_name.find("_")]
            possible_attr_name = attr_name[attr_name.find("_") + 1 :]
            if possible_attr_name in self.attribute_lookup:
                attr_data = self.attribute_lookup[possible_attr_name]
                attr_data["LABEL"] = possible_label
            else:
                possible_label = attr_name[attr_name.rfind("_") + 1 :]
                possible_attr_name = attr_name[0 : attr_name.rfind("_")]
                if possible_attr_name in self.attribute_lookup:
                    attr_data = self.attribute_lookup[possible_attr_name]
                    attr_data["LABEL"] = possible_label
        if attr_data:
            self.mapped_attribute[attr_name] = attr_data
        else:
            self.mapped_attribute[attr_name] = {
                "ATTR_NAME": attr_name,
                "UNMAPPED": True,
            }

    def add_to_features(self, features, errors, parent, attr_name, attr_value):
        if isinstance(attr_value, (list, dict)):
            errors.append(f"Expected integer or string for {attr_name}")
        else:
            attr_data = self.mapped_attribute[attr_name].copy()
            attr_data["ATTR_VALUE"] = attr_value
            feature_key = f"{parent}|{attr_data['FTYPE_CODE'] if attr_data['FTYPE_CODE'] else attr_data['ATTR_CODE']}|{attr_data.get('LABEL', '')}"
            if feature_key not in features:
                features[feature_key] = [attr_data]
            else:
                features[feature_key].append(attr_data)

    def update_feature_stats(self, feature, attribute, value):
        if attribute in self.feature_stats[feature]["attributes"]:
            self.feature_stats[feature]["attributes"][attribute]["count"] += 1
        else:
            order = self.attribute_lookup[attribute]["ATTR_ID"]
            # order = 1004 if attribute == 'RECORD_TYPE' else order # until moved in 4.0
            # print(order, attribute)
            self.feature_stats[feature]["attributes"][attribute] = {
                "order": order,
                "count": 1,
                "values": {},
            }
        if value in self.feature_stats[feature]["attributes"][attribute]["values"]:
            self.feature_stats[feature]["attributes"][attribute]["values"][value] += 1
        elif (
            len(self.feature_stats[feature]["attributes"][attribute]["values"])
            < self.max_values_per_attr
        ):
            self.feature_stats[feature]["attributes"][attribute]["values"][value] = 1

    def update_unmapped_stats(self, attr_name, attr_value):
        if attr_name in self.unmapped_stats:
            self.unmapped_stats[attr_name]["count"] += 1
        else:
            self.unmapped_stats[attr_name] = {"count": 1, "values": {}}
        if attr_value in self.unmapped_stats[attr_name]["values"]:
            self.unmapped_stats[attr_name]["values"][attr_value] += 1
        elif (
            len(self.unmapped_stats[attr_name]["values"].keys())
            < self.max_values_per_attr
        ):
            self.unmapped_stats[attr_name]["values"][attr_value] = 1

    def update_message_stats(self, cat, stat, row_num="n/a"):
        row_num = f"row {row_num}" if isinstance(row_num, int) else row_num
        if stat not in self.message_stats[cat]:
            self.message_stats[cat][stat] = {"count": 1, "rows": [row_num]}
        else:
            self.message_stats[cat][stat]["count"] += 1
            if self.message_stats[cat][stat]["count"] < 100:
                self.message_stats[cat][stat]["rows"].append(row_num)

    def analyze_json(self, input_data, input_row_num=None):
        self.record_count += 1

        # print('-'*50)
        # print(json.dumps(input_data, indent=4))
        message_list = []
        features = {}
        for attr_name in input_data.keys():
            if not input_data[attr_name]:
                continue
            self.register_attribute(attr_name)
            attr_value = str(input_data[attr_name])

            # its certainly a feature attribute
            if not self.mapped_attribute[attr_name].get("UNMAPPED"):
                self.add_to_features(
                    features, message_list, "ROOT", attr_name, attr_value
                )
                continue

            # its a certainly an unmapped attribute because its not a list
            if not isinstance(input_data[attr_name], list):
                self.update_unmapped_stats(attr_name, str(attr_value))
                continue

            # its certainly unmapped if its not a list of dictionaries
            if not isinstance(input_data[attr_name][0], dict):
                self.update_unmapped_stats(attr_name, str(attr_value))
                continue

            # hopefully its a sub-list of features
            any_features = False
            unmapped_attributes = []
            child_instance = 0
            for child_data in input_data[attr_name]:
                child_instance += 1
                for child_attr_name in child_data.keys():
                    if not child_data[child_attr_name]:
                        continue
                    self.register_attribute(child_attr_name)
                    child_value = str(child_data[child_attr_name])

                    if not self.mapped_attribute[child_attr_name].get("UNMAPPED"):
                        any_features = True
                        self.add_to_features(
                            features,
                            message_list,
                            f"{attr_name}[{child_instance}]",
                            child_attr_name,
                            child_value,
                        )
                    else:
                        unmapped_attributes.append(
                            [f"{attr_name}->{child_attr_name}", child_value]
                        )

            # if no features, the whole list is unmapped
            if not any_features:
                self.update_unmapped_stats(attr_name, attr_value)
            else:
                for unmapped_attribute in unmapped_attributes:
                    self.update_unmapped_stats(
                        unmapped_attribute[0], str(unmapped_attribute[1])
                    )

        #        print(json.dumps(features, indent=4))

        features_mapped = []
        attributes_mapped = []
        for feature_key in features:
            parent, feature, label = feature_key.split("|")
            if feature in self.feature_stats:
                self.feature_stats[feature]["count"] += 1
            else:
                order = self.feature_order[feature]
                # if feature in self.feature_lookup:
                #    order = 200000 + self.feature_lookup[feature]['FTYPE_ID']
                # elif feature in self.attribute_lookup:
                #    order = 100000 + self.attribute_lookup[feature]['ATTR_ID']
                # else:
                #    order = -1
                self.feature_stats[feature] = {
                    "order": order,
                    "count": 1,
                    "values": {},
                    "attributes": {},
                }

            possible_complete_feature = False
            populated_attr_list = []
            populated_attr_values = []
            for attribute_data in sorted(
                features[feature_key], key=lambda k: k["ATTR_ID"]
            ):
                attribute = attribute_data["ATTR_CODE"]
                value = attribute_data["ATTR_VALUE"]
                if attribute_data["FELEM_CODE"] not in (
                    "USAGE_TYPE",
                    "USED_FROM_DT",
                    "USED_THRU_DT",
                ):
                    populated_attr_values.append(value)
                self.update_feature_stats(feature, attribute, value)
                populated_attr_list.append(attribute)
                if self.attribute_lookup[attribute]["FELEM_REQ"].upper() in (
                    "YES",
                    "ANY",
                ):
                    possible_complete_feature = True

            if (
                label
                and feature in self.label_to_attribute
                and self.label_to_attribute[feature] not in populated_attr_list
            ):
                self.update_feature_stats(
                    feature, self.label_to_attribute[feature], label
                )
                populated_attr_list.append(self.label_to_attribute[feature])

            if populated_attr_values:  # capture the full feature
                feature_desc = " ".join(populated_attr_values)
                if feature_desc not in self.feature_stats[feature]["values"]:
                    self.feature_stats[feature]["values"][feature_desc] = 1
                else:
                    self.feature_stats[feature]["values"][feature_desc] += 1

            attributes_mapped.extend(populated_attr_list)

            if (
                feature == "NAME"
                and "NAME_FULL" in populated_attr_list
                and any(
                    x in populated_attr_list
                    for x in ["NAME_ORG", "NAME_LAST", "NAME_FIRST"]
                )
            ):
                message_list.append(["INFO", f"Only NAME_FULL should be mapped"])
            if (
                feature == "ADDRESS"
                and "ADDR_FULL" in populated_attr_list
                and any(
                    x in populated_attr_list
                    for x in [
                        "ADDR_LINE1",
                        "ADDR_CITY",
                        "ADDR_STATE",
                        "ADDR_POSTAL_CODE",
                    ]
                )
            ):
                message_list.append(["INFO", f"Only ADDR_FULL should be mapped"])
            if (
                feature == "ADDRESS"
                and "ADDR_FULL" not in populated_attr_list
                and "ADDR_LINE1" not in populated_attr_list
            ):
                message_list.append(["INFO", f"Incomplete ADDRESS (no ADDR_LINE1)"])

            if feature in self.required_attributes:  # wont be for datasource, record_id
                for record in self.required_attributes[feature]:
                    if record["ATTR_CODE"] not in populated_attr_list:
                        if record["FELEM_REQ"] == "Yes":
                            message_list.append(
                                [
                                    "INFO",
                                    f"{record['ATTR_CODE']} required for complete {feature}",
                                ]
                            )
                            possible_complete_feature = False
                        elif record["FELEM_REQ"] == "Desired":
                            message_list.append(
                                ["INFO", f"{record['ATTR_CODE']} desired"]
                            )

            if possible_complete_feature:
                features_mapped.append(feature)

        if "DATA_SOURCE" not in input_data:
            message_list.append(["ERROR", "DATA_SOURCE required"])
        elif input_data["DATA_SOURCE"].upper() not in self.data_source_lookup:
            message_list.append(
                ["ERROR", f"DATA_SOURCE not found: {input_data['DATA_SOURCE']}"]
            )
        if "RECORD_ID" not in input_data:
            message_list.append(["WARNING", "RECORD_ID desired"])
        if "RECORD_TYPE" not in attributes_mapped:
            message_list.append(["INFO", "RECORD_TYPE missing"])

        if "NAME" not in features_mapped:
            message_list.append(["INFO", "NAME missing"])
        if "NAME" in features_mapped and len(features_mapped) == 1:
            message_list.append(["INFO", "Only NAME is mapped"])
            # this should be if F1 or FF or exclusive as well

        if "NAME_ORG" in attributes_mapped and any(
            x in attributes_mapped for x in ["NAME_LAST", "NAME_FIRST"]
        ):
            message_list.append(["WARNING", "PERSON AND ORG on same record"])

        # is dob parsable
        # companies should not have DOB, PASSPORT or DRLIC
        # people should not have business addresses

        # # --other warnings
        # if 'OTHER_ID' in mappedFeatures:
        #     if len(mappedFeatures['OTHER_ID']) > 1:
        #         messages.append(['INFO', 'Multiple other_ids mapped'])
        #     else:
        #         messages.append(['INFO', 'Use of other_id feature'])

        for message in message_list:
            self.update_message_stats(message[0], message[1], input_row_num)

        # print(json.dumps(self.feature_stats, indent=4))
        # input('wait')

    def get_report(self):
        table_headers = [
            "Category",
            "Attribute",
            "Record Count",
            "Record Percent",
            "Unique Count",
            "Unique Percent",
            "Top Value1",
            "Top Value2",
            "Top Value3",
            "Top Value4",
            "Top Value5",
            "Top Value6",
            "Top Value7",
            "Top Value8",
            "Top Value9",
            "Top Value10",
        ]
        table_rows = [table_headers]
        for feature in sorted(
            self.feature_stats.keys(), key=lambda k: self.feature_stats[k]["order"]
        ):
            row = ["" for x in range(len(table_headers))]
            row[0] = "MAPPED"
            row[1] = feature
            row[2] = self.feature_stats[feature]["count"]
            row[3] = round(
                self.feature_stats[feature]["count"] / self.record_count * 100.00, 2
            )
            row[4] = len(self.feature_stats[feature]["values"])
            row[5] = round(row[4] / row[2] * 100.00, 1)

            # warn of low population or uniqueness
            self.low_population_percent = 25
            self.low_f1_unique_percent = 80
            self.low_ff_unique_percent = 60
            self.low_fme_unique_percent = 50
            if self.feature_lookup.get(feature):
                if row[3] <= self.low_population_percent:
                    self.update_message_stats(
                        "WARNING",
                        f"{feature} < {self.low_population_percent}% populated",
                    )
                if (
                    self.feature_lookup[feature]["FTYPE_FREQ"] in ("A1", "F1")
                    and row[5] <= self.low_f1_unique_percent
                ):
                    self.update_message_stats(
                        "WARNING", f"{feature} < {self.low_f1_unique_percent}% unique"
                    )
                elif (
                    self.feature_lookup[feature]["FTYPE_FREQ"] == "FF"
                    and row[5] <= self.low_ff_unique_percent
                ):
                    self.update_message_stats(
                        "WARNING", f"{feature} < {self.low_f1_unique_percent}% unique"
                    )
                elif (
                    self.feature_lookup[feature]["FTYPE_FREQ"] == "FM"
                    and self.feature_lookup[feature]["FTYPE_FREQ"] == "Yes"
                    and row[5] <= self.low_fme_unique_percent
                ):
                    self.update_message_stats(
                        "WARNING", f"{feature} < {self.low_f1_unique_percent}% unique"
                    )

            i = 5
            for value in sorted(
                self.feature_stats[feature]["values"],
                key=lambda x: self.feature_stats[feature]["values"][x],
                reverse=True,
            ):
                display_value = value[0:97] + "..." if len(value) > 100 else value
                i += 1
                row[i] = (
                    f"{display_value} ({self.feature_stats[feature]['values'][value]})"
                )
                if i == len(row) - 1:
                    break
            table_rows.append(row)
            if (
                len(self.feature_stats[feature]["attributes"]) > 1
                or list(self.feature_stats[feature]["attributes"].keys())[0] != feature
            ):
                for attribute in sorted(
                    self.feature_stats[feature]["attributes"].keys(),
                    key=lambda k: self.feature_stats[feature]["attributes"][k]["order"],
                ):
                    row = ["" for x in range(len(table_headers))]
                    row[0] = "MAPPED"
                    row[1] = "  " + attribute
                    row[2] = self.feature_stats[feature]["attributes"][attribute][
                        "count"
                    ]
                    row[3] = round(
                        self.feature_stats[feature]["attributes"][attribute]["count"]
                        / self.record_count
                        * 100.00,
                        1,
                    )
                    row[4] = len(
                        self.feature_stats[feature]["attributes"][attribute]["values"]
                    )
                    row[5] = round(row[4] / row[2] * 100.00, 1)
                    i = 5
                    for value in sorted(
                        self.feature_stats[feature]["attributes"][attribute]["values"],
                        key=lambda x: self.feature_stats[feature]["attributes"][
                            attribute
                        ]["values"][x],
                        reverse=True,
                    ):
                        display_value = (
                            value[0:97] + "..." if len(value) > 100 else value
                        )
                        i += 1
                        row[i] = (
                            f"{display_value} ({self.feature_stats[feature]['attributes'][attribute]['values'][value]})"
                        )
                        if i == len(row) - 1:
                            break
                    table_rows.append(row)

        table_rows.append(["" for x in range(len(table_headers))])
        for attribute in sorted(self.unmapped_stats.keys()):
            row = ["" for x in range(len(table_headers))]
            row[0] = "UNMAPPED"
            row[1] = attribute
            row[2] = self.unmapped_stats[attribute]["count"]
            row[3] = round(
                self.unmapped_stats[attribute]["count"] / self.record_count * 100.00, 1
            )
            row[4] = len(self.unmapped_stats[attribute]["values"])
            row[5] = round(row[4] / row[2] * 100.00, 1)
            i = 5
            for value in sorted(
                self.unmapped_stats[attribute]["values"],
                key=lambda x: self.unmapped_stats[attribute]["values"][x],
                reverse=True,
            ):
                display_value = value[0:97] + "..." if len(value) > 100 else value
                i += 1
                row[i] = (
                    f"{display_value} ({self.unmapped_stats[attribute]['values'][value]})"
                )
                if i == len(row) - 1:
                    break
            table_rows.append(row)

        # reclass info to warning if higher than threshold percent
        old_category = "INFO"
        new_category = "WARNING"
        reclass_message_list = []
        for message in self.message_stats.get(old_category, {}).keys():
            if (
                self.message_stats[old_category][message]["count"] / self.record_count
                >= 0.25
            ):
                reclass_message_list.append(message)
        for message in reclass_message_list:
            self.message_stats[new_category][message] = self.message_stats[
                old_category
            ][message]
            del self.message_stats[old_category][message]

        for category in ["ERROR", "WARNING", "INFO"]:
            if category in self.message_stats:
                if not self.message_stats[category]:
                    continue
                table_rows.append(["" for x in range(len(table_headers))])
                for message in sorted(self.message_stats[category].keys()):
                    row = ["" for x in range(len(table_headers))]
                    row[0] = category
                    row[1] = message
                    if "<" in message:
                        row[2] = ""
                        row[3] = ""
                        row[4] = ""
                        row[5] = ""
                    else:
                        row[2] = self.message_stats[category][message]["count"]
                        row[3] = round(
                            self.message_stats[category][message]["count"]
                            / self.record_count
                            * 100.00,
                            1,
                        )
                        row[4] = ""
                        row[5] = ""
                        i = 5
                        for value in self.message_stats[category][message]["rows"]:
                            i += 1
                            row[i] = value
                            self.message_stats[category][message]["rows"]
                            if i == len(row) - 1:
                                break

                    table_rows.append(row)

        return table_rows


# =========================
class JsonlReader:
    def __init__(self, file_handle):
        self.file_handle = file_handle

    def __iter__(self):
        return self

    def __next__(self):  # Python 2: def next(self)
        return json.loads(next(self.file_handle))


# ----------------------------------------
def format_pretty_table(table_rows):
    table_object = prettytable.PrettyTable()
    table_object.horizontal_char = "\u2500"
    table_object.vertical_char = "\u2502"
    table_object.junction_char = "\u253c"

    colors = {
        "MAPPED": "\033[38;5;70m",
        "UNMAPPED": "\033[38;5;178m",
        "ERROR": "\033[38;5;124m",
        "WARNING": "\033[38;5;202m",
        "INFO": "\033[38;5;39m",
        "HEADER": "\033[38;5;242m",
        "DIM": "\033[02m",
        "RESET": "\033[0m",
    }

    # find notable errors and warnings to colorize
    missing_data_sources = []
    low_population_features = []
    low_unique_features = []
    for message in analyzer.message_stats.get("ERROR", {}):
        if message.startswith("DATA_SOURCE not found: "):
            missing_data_sources.append(message.split()[-1])
    for message in analyzer.message_stats.get("WARNING", {}):
        if "populated" in message:
            low_population_features.append(message.split()[0])
        elif "unique" in message:
            low_unique_features.append(message.split()[0])

    table_object.field_names = [
        f"{colors['HEADER']}{x}{colors['RESET']}" for x in table_rows[0]
    ]
    for orig_row in table_rows[1:]:
        row = orig_row.copy()  # so outer table not changed
        if orig_row[0] == "MAPPED":
            if row[1][0:1] == " ":
                row[1] = f"{colors[row[0]]+colors['DIM']}{row[1]}{colors['RESET']}"
                i = 2
                while i < len(row):
                    row[i] = f"{colors['DIM']}{row[i]}{colors['RESET']}"
                    i += 1
            else:
                row[1] = f"{colors[row[0]]}{row[1]}{colors['RESET']}"

            # colorize any low population percents
            if orig_row[1] in low_population_features:
                row[3] = f"{colors['WARNING']}{row[3]}{colors['RESET']}"
            if orig_row[1] in low_unique_features:
                row[5] = f"{colors['WARNING']}{row[5]}{colors['RESET']}"

            # colorize missing data sources
            if orig_row[1] == "DATA_SOURCE":
                i = 5
                while i < len(row) - 1:
                    i += 1
                    if any(
                        row[i].startswith(missing_ds)
                        for missing_ds in missing_data_sources
                    ):
                        row[i] = f"{colors['ERROR']}{row[i]}{colors['RESET']}"

        elif row[0]:
            row[1] = f"{colors[row[0]]}{row[1]}{colors['RESET']}"
        if row[0]:  # --update row[0] last so colors work!
            row[0] = f"{colors[row[0]]}{row[0]}{colors['RESET']}"
        table_object.add_row(row)

    for field_name in table_object.field_names:
        if field_name.endswith("Count") or field_name.endswith("Percent"):
            table_object.align[field_name] = "r"
        else:
            table_object.align[field_name] = "l"

    return table_object.get_string()


# ----------------------------------------
def format_csv_table(table_rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(table_rows)
    return output.getvalue()


# ----------------------------------------
def print_report(report_string):
    less = subprocess.Popen(["less", "-FMXSR"], stdin=subprocess.PIPE)
    try:
        less.stdin.write(report_string.encode("utf-8"))
    except IOError:
        pass
    less.stdin.close()
    less.wait()
    print()


# ----------------------------------------
def signal_handler(signal, frame):
    print("USER INTERRUPT! Shutting down ... (please wait)")
    global shut_down
    shut_down = True
    return


# ----------------------------------------
if __name__ == "__main__":
    shut_down = False
    signal.signal(signal.SIGINT, signal_handler)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-i", "--input_file", dest="input_file", help="the name of the input file"
    )
    parser.add_argument(
        "-o",
        "--output_file",
        dest="output_file",
        help="optional name of the output file",
    )
    args = parser.parse_args()

    if not args.input_file or not os.path.exists(args.input_file):
        print("\nPlease supply a valid input file name on the command line\n")
        sys.exit(1)

    config_file_name = f"{os.path.dirname(os.path.abspath(sys.argv[0]))}{os.path.sep}sz_default_config.json"
    config_data, config_message = get_config_data(config_file_name)
    print(f"\n{config_message}\n")
    if not config_data:
        sys.exit(1)
    analyzer = SzJsonAnalyzer(config_data)

    input_file_handle = open(args.input_file, "r")
    input_file_ext = os.path.splitext(args.input_file)[1].upper()
    if input_file_ext == ".CSV":
        sniffer = csv.Sniffer().sniff(input_file_handle.readline(), delimiters="|,\t")
        input_file_handle.seek(0)
        delimiter = sniffer.delimiter
        if sniffer.delimiter == "\t":
            dialect = "excel-tab"
        elif sniffer.delimiter == "|":
            csv.register_dialect("pipe", delimiter="|", quotechar='"')
            dialect = "pipe"
        else:
            dialect = "excel"
        reader = csv.DictReader(input_file_handle, dialect=csv_dialect)
    else:
        reader = JsonlReader(input_file_handle)

    proc_start_time = time.time()
    input_row_count = 0
    for input_row in reader:
        input_row_count += 1
        analyzer.analyze_json(input_row, input_row_count)
        if input_row_count % 10000 == 0:
            eps = int(
                float(input_row_count)
                / (
                    float(
                        time.time() - proc_start_time
                        if time.time() - proc_start_time != 0
                        else 0
                    )
                )
            )
            print(f"{input_row_count:,} rows processed at {eps:,} per second")
        if shut_down:
            break

    elapsed_mins = round((time.time() - proc_start_time) / 60, 1)
    run_status = (
        "completed in" if not shut_down else "aborted after"
    ) + " %s minutes" % elapsed_mins
    print(f"{input_row_count:,} rows processed, {run_status}\n")
    input_file_handle.close()

    print("\ncreating report ...\n")
    report_table = analyzer.get_report()

    if prettytable:
        print_report(format_pretty_table(report_table))

    # --write statistics file
    if args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as outfile:
            outfile.write(format_csv_table(report_table))
        print(f"Report written to {args.output_file}\n")
    elif not prettytable:
        print(report_table)
        print()
    sys.exit(0)
