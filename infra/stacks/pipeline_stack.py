"""All AWS resources for the fraud pipeline in one stack.

Cost notes: everything is pay-per-request except the single provisioned
Kinesis shard (~$0.015/hour). `cdk destroy` removes it all, including the
data bucket (auto_delete_objects) -- this is a demo, not a bank.
"""
from pathlib import Path

from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrations,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_kinesis as kinesis,
    aws_kinesisfirehose as firehose,
    aws_lambda as lambda_,
    aws_lambda_event_sources as sources,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_sqs as sqs,
)
from constructs import Construct

SRC = str(Path(__file__).resolve().parents[2] / "src")


class FraudPipelineStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, alert_email: str | None = None,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------ ingest
        stream = kinesis.Stream(
            self, "Transactions", stream_name="fraud-transactions",
            stream_mode=kinesis.StreamMode.PROVISIONED, shard_count=1,
            retention_period=Duration.hours(24),
        )

        # ------------------------------------------------------------------ storage
        def table(name: str, pk: str, ttl: str | None = None) -> ddb.Table:
            return ddb.Table(
                self, name, partition_key=ddb.Attribute(name=pk, type=ddb.AttributeType.STRING),
                billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
                time_to_live_attribute=ttl, removal_policy=RemovalPolicy.DESTROY,
            )

        state_table = table("UserState", "user_id")
        alerts_table = table("Alerts", "txn_id", ttl="expires_at")
        connections_table = table("WsConnections", "connection_id", ttl="expires_at")

        lake = s3.Bucket(
            self, "DecisionLake", encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30))],
        )

        firehose_role = iam.Role(self, "FirehoseRole",
                                 assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"))
        lake.grant_read_write(firehose_role)
        delivery = firehose.CfnDeliveryStream(
            self, "DecisionsToS3", delivery_stream_type="DirectPut",
            extended_s3_destination_configuration=firehose.CfnDeliveryStream
            .ExtendedS3DestinationConfigurationProperty(
                bucket_arn=lake.bucket_arn, role_arn=firehose_role.role_arn,
                prefix="decisions/dt=!{timestamp:yyyy-MM-dd}/",
                error_output_prefix="errors/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/",
                buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                    interval_in_seconds=60, size_in_m_bs=5),
                compression_format="GZIP",
            ),
        )
        delivery.node.add_dependency(firehose_role)

        # ------------------------------------------------------------------ alerting
        topic = sns.Topic(self, "FraudAlerts", display_name="Fraud alerts")
        if alert_email:
            topic.add_subscription(subs.EmailSubscription(alert_email))

        # ------------------------------------------------------------------ websocket API
        code = lambda_.Code.from_asset(SRC, exclude=["**/__pycache__", "**/*.pyc"])
        common = dict(runtime=lambda_.Runtime.PYTHON_3_12, code=code,
                      architecture=lambda_.Architecture.ARM_64,
                      log_retention=logs.RetentionDays.ONE_WEEK)

        connect_fn = lambda_.Function(
            self, "WsConnect", handler="fraud_pipeline.aws.ws.connect",
            environment={"CONNECTIONS_TABLE": connections_table.table_name},
            timeout=Duration.seconds(5), memory_size=128, **common)
        disconnect_fn = lambda_.Function(
            self, "WsDisconnect", handler="fraud_pipeline.aws.ws.disconnect",
            environment={"CONNECTIONS_TABLE": connections_table.table_name},
            timeout=Duration.seconds(5), memory_size=128, **common)
        connections_table.grant_read_write_data(connect_fn)
        connections_table.grant_read_write_data(disconnect_fn)

        ws_api = apigwv2.WebSocketApi(
            self, "DashboardSocket",
            connect_route_options=apigwv2.WebSocketRouteOptions(
                integration=integrations.WebSocketLambdaIntegration("Connect", connect_fn)),
            disconnect_route_options=apigwv2.WebSocketRouteOptions(
                integration=integrations.WebSocketLambdaIntegration("Disconnect", disconnect_fn)),
        )
        ws_stage = apigwv2.WebSocketStage(self, "Prod", web_socket_api=ws_api,
                                          stage_name="prod", auto_deploy=True)

        # ------------------------------------------------------------------ scoring
        dlq = sqs.Queue(self, "ScoringDLQ", retention_period=Duration.days(14),
                        enforce_ssl=True)

        scorer = lambda_.Function(
            self, "Scorer", handler="fraud_pipeline.aws.handler.handler",
            memory_size=1024,  # more memory = more CPU for the tree walk
            timeout=Duration.seconds(30), tracing=lambda_.Tracing.ACTIVE,
            environment={
                "STATE_TABLE": state_table.table_name,
                "ALERTS_TABLE": alerts_table.table_name,
                "CONNECTIONS_TABLE": connections_table.table_name,
                "ALERT_TOPIC_ARN": topic.topic_arn,
                "FIREHOSE_STREAM": delivery.ref,
                "WS_CALLBACK_URL": ws_stage.callback_url,
            },
            **common,
        )
        state_table.grant_read_write_data(scorer)
        alerts_table.grant_read_write_data(scorer)
        connections_table.grant_read_write_data(scorer)
        topic.grant_publish(scorer)
        ws_api.grant_manage_connections(scorer)
        scorer.add_to_role_policy(iam.PolicyStatement(
            actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
            resources=[delivery.attr_arn]))

        scorer.add_event_source(sources.KinesisEventSource(
            stream,
            starting_position=lambda_.StartingPosition.LATEST,
            batch_size=100,
            max_batching_window=Duration.seconds(0),   # latency over batching efficiency
            parallelization_factor=2,                  # still ordered per partition key (user)
            bisect_batch_on_error=True,
            retry_attempts=3,
            max_record_age=Duration.hours(1),
            report_batch_item_failures=True,
            on_failure=sources.SqsDlq(dlq),
        ))

        # ------------------------------------------------------------------ observability
        iterator_age = stream.metric_get_records_iterator_age_milliseconds(
            statistic="Maximum", period=Duration.minutes(1))
        lag_alarm = cw.Alarm(
            self, "ConsumerLagAlarm", metric=iterator_age, threshold=60_000,
            evaluation_periods=3, alarm_description="Scorer is >60s behind the stream",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING)
        dlq_alarm = cw.Alarm(
            self, "DlqAlarm",
            metric=dlq.metric_approximate_number_of_messages_visible(period=Duration.minutes(1)),
            threshold=1, evaluation_periods=1,
            alarm_description="Records failed after all retries; run scripts/replay_dlq.py",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING)
        for alarm in (lag_alarm, dlq_alarm):
            alarm.add_alarm_action(cw_actions.SnsAction(topic))

        def custom(name: str, stat: str = "Sum") -> cw.Metric:
            return cw.Metric(namespace="FraudPipeline", metric_name=name, statistic=stat,
                             period=Duration.minutes(1))

        dashboard = cw.Dashboard(self, "Dashboard", dashboard_name="fraud-pipeline")
        dashboard.add_widgets(
            cw.GraphWidget(title="Throughput (txns/min)", width=8,
                           left=[custom("Processed"), custom("Flagged"), custom("Blocked")]),
            cw.GraphWidget(title="Latency (ms)", width=8,
                           left=[custom("EndToEndLatencyMs", "p50"),
                                 custom("EndToEndLatencyMs", "p99"),
                                 custom("DecisionLatencyMs", "p99")]),
            cw.GraphWidget(title="Consumer lag (iterator age ms)", width=8, left=[iterator_age]),
        )
        dashboard.add_widgets(
            cw.GraphWidget(title="Scorer errors / throttles", width=8,
                           left=[scorer.metric_errors(), scorer.metric_throttles()]),
            cw.GraphWidget(title="Duplicates & malformed", width=8,
                           left=[custom("Duplicates"), custom("Malformed")]),
            cw.GraphWidget(title="DLQ depth", width=8,
                           left=[dlq.metric_approximate_number_of_messages_visible()]),
        )

        # ------------------------------------------------------------------ outputs
        CfnOutput(self, "StreamName", value=stream.stream_name)
        CfnOutput(self, "WebSocketUrl", value=ws_stage.url)
        CfnOutput(self, "DlqUrl", value=dlq.queue_url)
        CfnOutput(self, "DataLakeBucket", value=lake.bucket_name)
        CfnOutput(self, "AlertsTable", value=alerts_table.table_name)
