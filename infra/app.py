#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks.pipeline_stack import FraudPipelineStack

app = cdk.App()
FraudPipelineStack(
    app, "FraudPipeline",
    alert_email=app.node.try_get_context("alertEmail"),  # cdk deploy -c alertEmail=you@x.com
    env=cdk.Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"),
                        region=os.getenv("CDK_DEFAULT_REGION", "us-east-1")),
)
cdk.Tags.of(app).add("project", "fraud-detection-pipeline")
app.synth()
