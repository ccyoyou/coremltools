load("@rules_java//java:defs.bzl", "java_proto_library")
load("@rules_jvm_external//:defs.bzl", "java_export")
load("@rules_proto//proto:defs.bzl", "proto_library")
load("//:protobuf_version.bzl", "PROTOBUF_VERSION")
load("//java/internal:testing.bzl", "junit_tests")

java_library(
    name = "util",
    srcs = glob([
        "src/main/java/com/google/protobuf/util/*.java",
    ]),
    visibility = ["//visibility:public"],
    deps = [
        "//external:error_prone_annotations",
        "//external:j2objc_annotations",
        "//external:gson",
        "//external:jsr305",
        "//external:guava",
        "//java/core",
        "//java/lite",
    ],
)
# Bazel users, don't depend on this target, use :util.
java_export(
    name = "util_mvn",
    maven_coordinates = "com.google.protobuf:protobuf-java-util:%s" % PROTOBUF_VERSION,
    pom_template = "pom_template.xml",
    runtime_deps = [":util"],
    visibility = ["//java:__pkg__"],
)

filegroup(
    name = "release",
    visibility = ["//java:__pkg__"],
    srcs = [
        ":util_mvn-pom",
        ":util_mvn-maven-source",
        ":util_mvn-docs",
        ":util_mvn-project",
    ]
)

proto_library(
    name = "test_protos",
    srcs = glob(["src/test/proto/**/*.proto"]),
    deps = [
        "//:any_proto",
        "//:duration_proto",
        "//:field_mask_proto",
        "//:struct_proto",
        "//:timestamp_proto",
        "//:wrappers_proto",
    ],
)

java_proto_library(
    name = "test_protos_java_proto",
    deps = [":test_protos"],
)

junit_tests(
    name = "tests",
    srcs = glob(["src/test/java/**/*.java"]),
    package_name = "com.google.protobuf.util",
    deps = [
        ":test_protos_java_proto",
        ":util",
        "//external:guava",
        "//external:junit",
        "//external:truth",
        "//java/core",
        "//java/core:generic_test_protos_java_proto",
    ],
)
