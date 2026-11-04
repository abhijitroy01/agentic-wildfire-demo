import {
  Stack, StackProps, Duration, RemovalPolicy, CfnOutput, CfnParameter, CfnCondition, CfnMapping, Fn, Aws, Token,
  aws_s3 as s3, aws_ecr as ecr, aws_codebuild as codebuild, aws_cloudtrail as cloudtrail,
  aws_ec2 as ec2, aws_ecs as ecs, aws_elasticloadbalancingv2 as elb,
  aws_cloudfront as cloudfront, aws_cloudfront_origins as origins,
  aws_cognito as cognito, aws_dynamodb as dynamodb, aws_sqs as sqs,
  aws_logs as logs, aws_secretsmanager as secretsmanager, custom_resources as custom,
  aws_cloudwatch as cloudwatch, aws_wafv2 as waf,
  aws_bedrockagentcore as agentcore,
  aws_iam as iam, aws_lambda as lambda, aws_lambda_event_sources as sources,
  aws_events as events, aws_events_targets as targets,
} from "aws-cdk-lib";
import { Construct } from "constructs";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  nag, noResourceLevel, regionAndAccount, s3AccessLogReason, s3GrantActions, s3GrantReason, s3WriteActions,
} from "./nag";
import { denyObjectAccessOutsideInstallation, operatorRoles } from "./bucket-access";

const integrationContract = JSON.parse(readFileSync(
  join(__dirname, "../../../schemas/simulation/capabilities.json"), "utf8",
)) as { api_version: string; capability_version: string };

// The captured Python runner owns Docker recipes, fixed groups and verification-before-push ordering.
export const imageBuildCommands = ["python3 deployment/container_build.py"];

// Requests that start work (D20), as the API load balancer receives them after CloudFront: v1 runs, variants and
// draft preparation, Traffic Lab runs and lookups, agent and investigation turns, radio streams, and firetruck runs
// and reports. WAF matches the URL-decoded, lower-cased path, because the origin percent-decodes before routing.
// deployment/tests/test_edge_rules.py checks this pattern against every POST route of the workspace API.
export const workStartingPaths = "^/api/v1/(runs|variants|scenario-drafts|agent/turns|investigation/turns|incidents/[^/]+/investigation|radio/stream|research/(jobs|discover|sources)|firetruck/runs(/[^/]+/reports)?|fire-exposure/jobs/[^/]+/retry)/?$";
// WAF rate rules count requests over 5 minutes; a caller blocked by one is told to retry after that window.
const rateWindowSeconds = 300;
// WAF answers its own blocks in the API's v1 error envelope, so the UI shows error.message as for API errors.
const wafErrorBody = (code: string, message: string, retryable: boolean) => ({
  contentType: "APPLICATION_JSON",
  content: JSON.stringify({ error: { code, message, retryable, request_id: null, details: {} } }),
});
// The optional Delphire camera's [camera] rules, one rule set for three validators: the same pattern text is in
// deployment/installation.py and backend/api_settings.py, and
// deployment/tests/test_installation.py keeps the three equal. Protocol bounds, not operational settings (D38).
// Lower-case DNS labels; the last label starts with a letter, so an IPv4 host never matches.
export const CAMERA_ORIGIN = String.raw`https://([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?`;
// "https://" (8 characters) plus a host of at most 253.
export const CAMERA_ORIGIN_MAX_LENGTH = 261;
export const CAMERA_DEVICE_ID = String.raw`[A-Za-z0-9_-]{1,64}`;
export const CAMERA_ALIASES = ["camera_a", "camera_b", "infrared_camera_a", "infrared_camera_b", "test_camera_a"];
// Fixed (D37, D53): the newest 10 images: the current and previous release and up to eight unaccepted builds;
// deployment/retention.py keeps the same number.
export const IMAGES_KEPT = 10;

/** prefix: the installation's resource prefix (bin/app.ts), which also names the backend root stack. */
interface FoundationProps extends StackProps { prefix?: string }
export class Foundation extends Stack {
  /** Workspace API image (construct ID "Images" is kept so installed repositories are retained). */
  readonly repository: ecr.Repository;
  readonly trafficRepository: ecr.Repository;
  readonly sourceBucket: s3.Bucket;
  readonly builder: codebuild.Project;
  readonly firetruckRepository: ecr.Repository;
  readonly radioRepository: ecr.Repository;
  readonly fireRiskRepository: ecr.Repository;
  readonly nativeRepository: ecr.Repository;
  constructor(scope: Construct, id: string, props: FoundationProps) {
    // On unless a caller turns it off (D53): the CDK CLI applies the artifact's value on every deploy.
    super(scope, id, { ...props, terminationProtection: props.terminationProtection ?? true });
    // Every bucket here blocks public access, accepts only TLS (enforceSSL) and disables ACLs (BucketOwnerEnforced, the
    // S3 default for new buckets, stated so the template shows it; CloudTrail's bucket-owner-full-control writes work).
    const audit = new s3.Bucket(this, "Audit", {
      encryption: s3.BucketEncryption.S3_MANAGED, enforceSSL: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, versioned: true,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED, removalPolicy: RemovalPolicy.RETAIN,
      lifecycleRules: [{
        // Fixed retention, not a setting (D37): a year of CloudTrail logs covers a review of the past year's activity.
        expiration: Duration.days(365),
        // Fixed (D37): an overwritten or deleted log file stays recoverable for 90 days after the change.
        noncurrentVersionExpiration: Duration.days(90),
      }],
    });
    // Every Region and the global services (IAM, STS, CloudFront), so the account's control-plane calls are all recorded;
    // digest files make any edit to a delivered log file detectable.
    const trail = new cloudtrail.Trail(this, "Trail", {
      bucket: audit, isMultiRegionTrail: true, enableFileValidation: true,
      includeGlobalServiceEvents: true, managementEvents: cloudtrail.ReadWriteType.ALL,
    });
    this.sourceBucket = new s3.Bucket(this, "BuildSource", {
      encryption: s3.BucketEncryption.S3_MANAGED, enforceSSL: true, versioned: true,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, removalPolicy: RemovalPolicy.RETAIN,
      // Fixed retention, not a setting (D37): a replaced or deleted source bundle stays recoverable for 30 days, long
      // enough to rebuild or resume a recent release from its exact S3 version.
      lifecycleRules: [{ noncurrentVersionExpiration: Duration.days(30) }],
    });
    // AES-256 at rest, not KMS (D3, accepted risk): ECR cannot change an existing repository's encryption, so moving
    // to a KMS key would replace each repository and delete its images.
    // ECR expires all but the newest images; the deployer refuses a release whose push would expire one in use.
    const repository = (name: string) => new ecr.Repository(this, name, {
      imageScanOnPush: true, imageTagMutability: ecr.TagMutability.IMMUTABLE,
      encryption: ecr.RepositoryEncryption.AES_256, removalPolicy: RemovalPolicy.RETAIN,
      lifecycleRules: [{ description: "Keep the newest 10 release images (D53)", tagStatus: ecr.TagStatus.ANY,
        maxImageCount: IMAGES_KEPT }],
    });
    this.repository = repository("Images");
    this.trafficRepository = repository("TrafficSimulatorImages");
    this.firetruckRepository = repository("FiretruckImages");
    this.radioRepository = repository("RadioImages");
    this.fireRiskRepository = repository("FireRiskImages");
    // Disposable compiler outputs live apart from deployable images. Expiry only causes recompilation.
    this.nativeRepository = new ecr.Repository(this, "NativeBuildArtifacts", {
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      encryption: ecr.RepositoryEncryption.AES_256, removalPolicy: RemovalPolicy.RETAIN,
      lifecycleRules: [{ description: "Expire unused native compiler artifacts after 30 days",
        tagStatus: ecr.TagStatus.ANY, maxImageAge: Duration.days(30) }],
    });
    new CfnOutput(this, "NativeBuildRepositoryUri", {value: this.nativeRepository.repositoryUri});
    new CfnOutput(this, "FireRiskRepositoryUri", {value: this.fireRiskRepository.repositoryUri});
    new CfnOutput(this, "FireRiskRepositoryArn", {value: this.fireRiskRepository.repositoryArn});
    new CfnOutput(this, "TrafficSimulatorRepositoryUri", {value: this.trafficRepository.repositoryUri});
    new CfnOutput(this, "FiretruckRepositoryUri", {value: this.firetruckRepository.repositoryUri});
    new CfnOutput(this, "RadioRepositoryUri", {value: this.radioRepository.repositoryUri});
    const buildLogs = new logs.LogGroup(this, "BuildLogs", {
      retention: logs.RetentionDays.ONE_MONTH, removalPolicy: RemovalPolicy.RETAIN,
    });
    this.builder = new codebuild.Project(this, "ImageBuilder", {
      // Deployment supplies a content-addressed key and exact S3 version for every build.
      source: codebuild.Source.s3({ bucket: this.sourceBucket, path: "sources/deployment-required.zip" }),
      environment: { buildImage: codebuild.LinuxBuildImage.STANDARD_7_0,
        privileged: true, computeType: codebuild.ComputeType.LARGE },
      concurrentBuildLimit: 2,
      // Best effort only: durable native reuse is through exact-digest ECR artifacts.
      cache: codebuild.Cache.local(codebuild.LocalCacheMode.DOCKER_LAYER),
      environmentVariables: {
        REPOSITORY_URI: { value: this.repository.repositoryUri },
        TRAFFIC_REPOSITORY_URI: { value: this.trafficRepository.repositoryUri },
        FIRETRUCK_REPOSITORY_URI: {value: this.firetruckRepository.repositoryUri},
        RADIO_REPOSITORY_URI: {value: this.radioRepository.repositoryUri},
        FIRE_RISK_REPOSITORY_URI: {value: this.fireRiskRepository.repositoryUri},
        NATIVE_REPOSITORY_URI: {value: this.nativeRepository.repositoryUri},
      },
      timeout: Duration.minutes(60), queuedTimeout: Duration.minutes(30),
      logging: { cloudWatch: { logGroup: buildLogs } },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: "0.2",
        phases: {
          pre_build: { commands: [
            'test -n "$IMAGE_TAG"',
            'aws ecr get-login-password --region "$AWS_DEFAULT_REGION" | docker login --username AWS --password-stdin "${REPOSITORY_URI%%/*}"',
            // Service images build from their own folders; give each the reviewed repository lock.
            "cp requirements-lock.txt firetruck-simulator/requirements-lock.txt",
            "cp requirements-lock.txt broadcastify/requirements-lock.txt",
          ] },
          build: { commands: imageBuildCommands },
          post_build: { commands: [
            'test "$CODEBUILD_BUILD_SUCCEEDING" = "1"',
          ] },
        },
      }),
    });
    for (const images of [this.repository, this.trafficRepository, this.firetruckRepository,
      this.radioRepository, this.fireRiskRepository, this.nativeRepository]) {
      images.grantPullPush(this.builder);
    }
    // The builder only reads the pinned source bundle; it writes nothing back to the source bucket.
    this.sourceBucket.grantRead(this.builder, "sources/*");
    // Only the installation's own roles, the operators and AWS services read or write these objects (D52). The
    // backend's Runtime build reads its source here as a role with a fixed name (4-agentcore-resources.yaml
    // CodeBuildServiceRole); the backend root stack is <prefix>-backend, and an app without a prefix has no backend.
    const operators = operatorRoles(this);
    const runtimeBuild = props.prefix ? [this.formatArn({ service: "iam", region: "", resource: "role",
      resourceName: `${props.prefix}-backend-runtime-build` })] : [];
    denyObjectAccessOutsideInstallation(audit, [], operators);
    denyObjectAccessOutsideInstallation(this.sourceBucket, [this.builder.role!.roleArn, ...runtimeBuild], operators);
    // The application stack is created after this one, so the deployer passes its site bucket's name (D53; doc 29's
    // rule that cross-stack bindings are explicit deployer parameters), "" before that stack exists.
    const siteBucket = new CfnParameter(this, "WorkspaceAssetsBucketName", {
      type: "String", allowedPattern: "([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])?",
      description: "The application stack's WorkspaceAssetsBucket output, whose object writes the trail records; \"\" before the application stack exists.",
    });
    const hasSiteBucket = new CfnCondition(this, "HasWorkspaceAssetsBucket", {
      expression: Fn.conditionNot(Fn.conditionEquals(siteBucket.valueAsString, "")),
    });
    // Object writes to the site and build-source buckets, write-only (D53): who changed a page or a source bundle.
    trail.addEventSelector(cloudtrail.DataResourceType.S3_OBJECT, [
      `${this.sourceBucket.bucketArn}/`,
      Token.asString(Fn.conditionIf(hasSiteBucket.logicalId, `arn:${Aws.PARTITION}:s3:::${siteBucket.valueAsString}/`,
        Aws.NO_VALUE)),
    ], { readWriteType: cloudtrail.ReadWriteType.WRITE_ONLY, includeManagementEvents: false });
    for (const bucket of [audit, this.sourceBucket]) nag(bucket, "AwsSolutions-S1", s3AccessLogReason);
    nag(this.builder, "AwsSolutions-CB4", "Accepted (D38, as G41): CodeBuild encrypts this project's build data " +
      "with AWS managed keys, and a customer-managed key would add a KMS key and its policy to the stack; the " +
      "project produces no build artifacts (images go to ECR) and logs only to the BuildLogs group.");
    nag(this.builder, "AwsSolutions-IAM5", s3GrantReason,
      [...s3GrantActions, { regex: "/^Resource::<BuildSource[0-9A-F]+\\.Arn>\\/sources\\/\\*$/g" }]);
    nag(this.builder, "AwsSolutions-IAM5", noResourceLevel("ecr:GetAuthorizationToken",
      "image pulls and pushes are limited to the five repositories."), ["Resource::*"]);
    nag(this.builder, "AwsSolutions-IAM5", "CDK's default CodeBuild grants, limited to this project's own report " +
      "groups and /aws/codebuild log group (the build itself logs to BuildLogs).", [
      { regex: `/^Resource::arn:<AWS::Partition>:codebuild:${regionAndAccount}:report-group\\/<ImageBuilder[0-9A-F]+>-\\*$/g` },
      { regex: `/^Resource::arn:<AWS::Partition>:logs:${regionAndAccount}:log-group:\\/aws\\/codebuild\\/<ImageBuilder[0-9A-F]+>:\\*$/g` },
    ]);
    new CfnOutput(this, "SourceBucket", { value: this.sourceBucket.bucketName });
    new CfnOutput(this, "RepositoryUri", { value: this.repository.repositoryUri });
    new CfnOutput(this, "RepositoryName", { value: this.repository.repositoryName });
    new CfnOutput(this, "RepositoryArn", { value: this.repository.repositoryArn });
    new CfnOutput(this, "TrafficSimulatorRepositoryArn", {value: this.trafficRepository.repositoryArn});
    new CfnOutput(this, "FiretruckRepositoryArn", {value: this.firetruckRepository.repositoryArn});
    new CfnOutput(this, "RadioRepositoryArn", {value: this.radioRepository.repositoryArn});
    new CfnOutput(this, "BuildProject", { value: this.builder.projectName });
  }
}

interface ApplicationProps extends StackProps { foundation: Foundation; imageTag: string; prefix?: string }
export class Application extends Stack {
  constructor(scope: Construct, id: string, props: ApplicationProps) {
    super(scope, id, { ...props, terminationProtection: props.terminationProtection ?? true });
    const simulationRuntime = new CfnParameter(this, "SimulationRuntimeArn", {
      type: "String", default: "", description: "Existing FireGUard AgentCore Runtime ARN; bind after backend deployment.",
    });
    const runtimeEndpoint = new CfnParameter(this, "SimulationRuntimeEndpoint", {
      type: "String", default: "RELEASE_A", allowedValues: ["RELEASE_A", "RELEASE_B"],
      description: "Accepted Runtime endpoint slot; change only after candidate readiness and verification.",
    });
    const endpointVersions = ["A", "B"].map(slot => new CfnParameter(this, `RuntimeEndpoint${slot}Version`, {
      type: "String", default: "1", allowedPattern: "^[1-9][0-9]{0,4}$",
      description: `Explicit version for RELEASE_${slot}; update only the inactive slot.`,
    }));
    const geocoder = new CfnParameter(this, "GeocoderFunctionArn", {
      type: "String", default: "", description: "Shared IAM-only Amazon Location geocoder; bind after backend deployment.",
    });
    const weather = new CfnParameter(this, "WeatherFunctionArn", {
      type: "String", default: "", description: "Existing IAM-only DTN weather Lambda; bind after backend deployment.",
    });
    const hasWeather = new CfnCondition(this, "HasWeather", {
      expression: Fn.conditionNot(Fn.conditionEquals(weather.valueAsString, "")),
    });
    const hasGeocoder = new CfnCondition(this, "HasGeocoder", {
      expression: Fn.conditionNot(Fn.conditionEquals(geocoder.valueAsString, "")),
    });
    // There is no user sign-in: "true" opens the workspace to anonymous demo sessions through CloudFront (a staging
    // installation is "true" too, and its WAF allowlist admits only the listed networks), and "false" keeps every
    // workspace route closed (a new installation's first pass, before the backend is bound).
    const publicDemo = new CfnParameter(this, "PublicDemoMode", {
      type: "String", default: "false", allowedValues: ["true", "false"],
      description: "Open the workspace to anonymous HttpOnly demo sessions through CloudFront; false keeps it closed.",
    });
    // The installation file's optional [camera] section. CloudFormation matches an allowed pattern against the whole
    // value, and (...)? admits the "" an installation without a camera passes. The camera key is never a parameter:
    // the API reads it from the provider secret.
    const camera = {
      CAMERA_API_BASE_URL: new CfnParameter(this, "CameraApiBaseUrl", {
        type: "String", default: "", allowedPattern: `(${CAMERA_ORIGIN})?`, maxLength: CAMERA_ORIGIN_MAX_LENGTH,
        description: "[camera] api_base_url: the Delphire API origin (https:// and a host name), or \"\" without a camera. The key is never a parameter.",
      }),
      CAMERA_DEVICE_ID: new CfnParameter(this, "CameraDeviceId", {
        type: "String", default: "", allowedPattern: `(${CAMERA_DEVICE_ID})?`,
        description: "[camera] device_id: the Delphire device the workspace API serves; \"\" keeps the camera off.",
      }),
      CAMERA_ALIAS: new CfnParameter(this, "CameraAlias", {
        type: "String", default: "", allowedPattern: `(${CAMERA_ALIASES.join("|")})?`,
        description: "[camera] camera_alias: the device's camera the workspace API serves, or \"\" without a camera.",
      }),
    };
    // Operational settings come only from deployment/settings.toml (D23): no default here, and the deployer
    // validates the range before passing the value, so every deploy applies exactly what the file says.
    const requestsPerIp = new CfnParameter(this, "RequestsPerIpPer5Minutes", {
      type: "Number",
      description: "rate_limits.requests_per_ip_per_5_minutes in deployment/settings.toml: WAF per-IP limit for all API requests in any 5-minute window.",
    });
    const workRequestsPerIp = new CfnParameter(this, "WorkRequestsPerIpPer5Minutes", {
      type: "Number",
      description: "rate_limits.work_requests_per_ip_per_5_minutes in deployment/settings.toml: WAF per-IP limit for requests that start work in any 5-minute window.",
    });
    // Limits the containers read (D20, D23), each as the upper-case settings key. Both images import
    // traffic_simulator.settings, which checks its limits at start-up, so both tasks get those; the API task alone
    // gets the backend.api_settings limits. The settings module rejects a missing or out-of-range value by name.
    const simulatorLimits = {
      ARTIFACT_WRITE_MAX_ATTEMPTS: new CfnParameter(this, "ArtifactWriteMaxAttempts", { type: "Number",
        description: "deployment/settings.toml: artifact write max attempts." }),
      TOMTOM_CALLS_PER_DAY: new CfnParameter(this, "TomtomCallsPerDay", { type: "Number",
        description: "budgets.tomtom_calls_per_day in deployment/settings.toml: TomTom requests all visitors may cause per UTC day." }),
      TOMTOM_REQUESTS_PER_RUN: new CfnParameter(this, "TomtomRequestsPerRun", { type: "Number",
        description: "simulation.tomtom_requests_per_run in deployment/settings.toml: TomTom readings one traffic-speed run captures." }),
      TOMTOM_RESPONSE_MAX_AGE_SECONDS: new CfnParameter(this, "TomtomResponseMaxAgeSeconds", { type: "Number",
        description: "simulation.tomtom_response_max_age_seconds in deployment/settings.toml: oldest TomTom response date accepted at retrieval." }),
      TOMTOM_CACHE_MINUTES: new CfnParameter(this, "TomtomCacheMinutes", { type: "Number",
        description: "cache.tomtom_cache_minutes in deployment/settings.toml: how long a process reuses a TomTom reading." }),
      DEMO_SESSION_DAYS: new CfnParameter(this, "DemoSessionDays", { type: "Number",
        description: "capacity.demo_session_days in deployment/settings.toml: demo session and record lifetime before TTL deletion." }),
      // The API bounds requested study areas; the worker bounds road-aware preparation.
      MAX_STUDY_RADIUS_KM: new CfnParameter(this, "MaxStudyRadiusKm", { type: "Number",
        description: "simulation.max_study_radius_km in deployment/settings.toml: largest study-area radius the API and road worker may prepare." }),
      // The API and the worker both run Caltrans highway discoveries.
      CALTRANS_RESPONSE_MAX_MB: new CfnParameter(this, "CaltransResponseMaxMb", { type: "Number",
        description: "simulation.caltrans_response_max_mb in deployment/settings.toml: largest Caltrans highway-service response read, in MB." }),
    };
    // The firetruck service stops a run at this age, and the API's broker then stops counting it as active.
    const firetruckRunLifetime = new CfnParameter(this, "FiretruckRunLifetimeMinutes", { type: "Number",
      description: "capacity.firetruck_run_lifetime_minutes in deployment/settings.toml: longest a firetruck run lasts, in minutes." });
    const apiLimits = {
      DTN_MAX_ATTEMPTS: new CfnParameter(this, "DtnMaxAttempts", { type: "Number",
        description: "deployment/settings.toml: dtn max attempts." }),
      DTN_OPERATION_TIMEOUT_SECONDS: new CfnParameter(this, "DtnOperationTimeoutSeconds", { type: "Number",
        description: "deployment/settings.toml: dtn operation timeout seconds." }),
      DTN_REQUEST_TIMEOUT_SECONDS: new CfnParameter(this, "DtnRequestTimeoutSeconds", { type: "Number",
        description: "deployment/settings.toml: dtn request timeout seconds." }),
      DTN_SERIALIZATION_RESERVE_SECONDS: new CfnParameter(this, "DtnSerializationReserveSeconds", { type: "Number",
        description: "deployment/settings.toml: dtn serialization reserve seconds." }),
      AGENT_REQUESTS_PER_DAY: new CfnParameter(this, "AgentRequestsPerDay", { type: "Number",
        description: "budgets.agent_requests_per_day in deployment/settings.toml: AgentCore agent requests all visitors may start per UTC day." }),
      GEOCODING_REQUESTS_PER_DAY: new CfnParameter(this, "GeocodingRequestsPerDay", { type: "Number",
        description: "budgets.geocoding_requests_per_day in deployment/settings.toml: Amazon Location lookups all visitors may start per UTC day." }),
      ACTIVE_JOBS_ALL_VISITORS: new CfnParameter(this, "ActiveJobsAllVisitors", { type: "Number",
        description: "capacity.active_jobs_all_visitors in deployment/settings.toml: active simulation jobs across all visitors." }),
      ACTIVE_JOBS_PER_SESSION: new CfnParameter(this, "ActiveJobsPerSession", { type: "Number",
        description: "capacity.active_jobs_per_session in deployment/settings.toml: active simulation jobs per demo session." }),
      DISCOVERIES_PER_API_PROCESS: new CfnParameter(this, "DiscoveriesPerApiProcess", { type: "Number",
        description: "capacity.discoveries_per_api_process in deployment/settings.toml: concurrent highway discoveries per API process." }),
      INVESTIGATIONS_PER_API_PROCESS: new CfnParameter(this, "InvestigationsPerApiProcess", { type: "Number",
        description: "capacity.investigations_per_api_process in deployment/settings.toml: background investigations per API process." }),
      REQUEST_BODY_MAX_KIB: new CfnParameter(this, "RequestBodyMaxKib", { type: "Number",
        description: "capacity.request_body_max_kib in deployment/settings.toml: largest API request body." }),
      REQUEST_BODY_SECONDS: new CfnParameter(this, "RequestBodySeconds", { type: "Number",
        description: "capacity.request_body_seconds in deployment/settings.toml: time allowed to send an API request body." }),
      DEMO_SESSIONS_PER_IP_PER_DAY: new CfnParameter(this, "DemoSessionsPerIpPerDay", { type: "Number",
        description: "rate_limits.demo_sessions_per_ip_per_day in deployment/settings.toml: new demo sessions per viewer IP per UTC day." }),
      // The firetruck broker's limits: a demo session's runs and live streams.
      FIRETRUCK_RUN_LIFETIME_MINUTES: firetruckRunLifetime,
      FIRETRUCK_ACTIVE_RUNS_PER_SESSION: new CfnParameter(this, "FiretruckActiveRunsPerSession", { type: "Number",
        description: "capacity.firetruck_active_runs_per_session in deployment/settings.toml: firetruck runs one demo session may have starting or running." }),
      FIRETRUCK_RUNS_PER_SESSION: new CfnParameter(this, "FiretruckRunsPerSession", { type: "Number",
        description: "capacity.firetruck_runs_per_session in deployment/settings.toml: firetruck runs one demo session may start." }),
      FIRETRUCK_STREAMS_PER_SESSION: new CfnParameter(this, "FiretruckStreamsPerSession", { type: "Number",
        description: "capacity.firetruck_streams_per_session in deployment/settings.toml: live firetruck streams one demo session may have open." }),
      FIRETRUCK_STREAM_LIFETIME_MINUTES: new CfnParameter(this, "FiretruckStreamLifetimeMinutes", { type: "Number",
        description: "capacity.firetruck_stream_lifetime_minutes in deployment/settings.toml: longest one live firetruck stream stays open, in minutes." }),
      // The camera broker's limits; the camera itself is the installation file's optional [camera] section.
      CAMERA_CALLS_PER_DAY: new CfnParameter(this, "CameraCallsPerDay", { type: "Number",
        description: "budgets.camera_calls_per_day in deployment/settings.toml: Delphire camera calls all visitors may cause per UTC day." }),
      CAMERA_CACHE_SECONDS: new CfnParameter(this, "CameraCacheSeconds", { type: "Number",
        description: "cache.camera_cache_seconds in deployment/settings.toml: how long an API process reuses the camera's latest records and image." }),
      CAMERA_IMAGE_MAX_KIB: new CfnParameter(this, "CameraImageMaxKib", { type: "Number",
        description: "capacity.camera_image_max_kib in deployment/settings.toml: largest camera image the broker serves, in KiB." }),
      CAMERA_REQUEST_TIMEOUT_SECONDS: new CfnParameter(this, "CameraRequestTimeoutSeconds", { type: "Number",
        description: "capacity.camera_request_timeout_seconds in deployment/settings.toml: longest one Delphire camera call may take." }),
    };
    const fireRiskLimits = {
      FIRE_RISK_LANDSCAPE_IDLE_TIMEOUT_SECONDS: new CfnParameter(this, "FireRiskLandscapeIdleTimeoutSeconds", { type: "Number",
        description: "deployment/settings.toml: fire risk landscape idle timeout seconds." }),
      FIRE_RISK_LANDSCAPE_MAX_ATTEMPTS: new CfnParameter(this, "FireRiskLandscapeMaxAttempts", { type: "Number",
        description: "deployment/settings.toml: fire risk landscape max attempts." }),
      FIRE_RISK_MODEL_ADMISSION_SECONDS: new CfnParameter(this, "FireRiskModelAdmissionSeconds", { type: "Number",
        description: "deployment/settings.toml: fire risk model admission seconds." }),
      FIRE_RISK_RETRY_MAX_SECONDS: new CfnParameter(this, "FireRiskRetryMaxSeconds", { type: "Number",
        description: "deployment/settings.toml: fire risk retry max seconds." }),
      FIRE_RISK_ASSESSMENTS_PER_DAY: new CfnParameter(this, "FireRiskAssessmentsPerDay", { type: "Number",
        description: "deployment/settings.toml: fire risk assessments per day." }),
      FIRE_RISK_WEATHER_CALLS_PER_DAY: new CfnParameter(this, "FireRiskWeatherCallsPerDay", { type: "Number",
        description: "deployment/settings.toml: fire risk weather calls per day." }),
      FIRE_RISK_MAX_FIRES_PER_TURN: new CfnParameter(this, "FireRiskMaxFiresPerTurn", { type: "Number",
        description: "deployment/settings.toml: fire risk max fires per turn." }),
      FIRE_RISK_POSTPROCESSING_SECONDS: new CfnParameter(this, "FireRiskPostprocessingSeconds", { type: "Number",
        description: "deployment/settings.toml: fire risk postprocessing seconds." }),
      FIRE_RISK_WEATHER_TIMEOUT_SECONDS: new CfnParameter(this, "FireRiskWeatherTimeoutSeconds", { type: "Number",
        description: "deployment/settings.toml: fire risk weather timeout seconds." }),
      FIRE_RISK_REQUEST_BODY_MAX_KIB: new CfnParameter(this, "FireRiskRequestBodyMaxKib", { type: "Number",
        description: "deployment/settings.toml: fire risk request body max kib." }),
      FIRE_RISK_RESPONSE_MAX_KIB: new CfnParameter(this, "FireRiskResponseMaxKib", { type: "Number",
        description: "deployment/settings.toml: fire risk response max kib." }),
      FIRE_RISK_PERIMETER_MAX_VERTICES: new CfnParameter(this, "FireRiskPerimeterMaxVertices", { type: "Number",
        description: "deployment/settings.toml: fire risk perimeter max vertices." }),
      FIRE_RISK_CALCULATIONS_PER_PROCESS: new CfnParameter(this, "FireRiskCalculationsPerProcess", { type: "Number",
        description: "deployment/settings.toml: fire risk calculations per process." }),
      FIRE_RISK_CALCULATION_TIMEOUT_SECONDS: new CfnParameter(this, "FireRiskCalculationTimeoutSeconds", { type: "Number",
        description: "deployment/settings.toml: fire risk calculation timeout seconds." }),
      FIRE_RISK_INNER_KM: new CfnParameter(this, "FireRiskInnerKm", { type: "Number",
        description: "deployment/settings.toml: fire risk inner km." }),
      FIRE_RISK_MIDDLE_KM: new CfnParameter(this, "FireRiskMiddleKm", { type: "Number",
        description: "deployment/settings.toml: fire risk middle km." }),
      FIRE_RISK_OUTER_KM: new CfnParameter(this, "FireRiskOuterKm", { type: "Number",
        description: "deployment/settings.toml: fire risk outer km." }),
      FIRE_RISK_RING_POINTS: new CfnParameter(this, "FireRiskRingPoints", { type: "Number",
        description: "deployment/settings.toml: fire risk ring points." }),
      FIRE_RISK_WIND_SHAPE_SCALE: new CfnParameter(this, "FireRiskWindShapeScale", { type: "Number",
        description: "deployment/settings.toml: fire risk wind shape scale." }),
      FIRE_RISK_MAX_WIND_STRETCH: new CfnParameter(this, "FireRiskMaxWindStretch", { type: "Number",
        description: "deployment/settings.toml: fire risk max wind stretch." }),
      FIRE_RISK_WEATHER_MAX_AGE_SECONDS: new CfnParameter(this, "FireRiskWeatherMaxAgeSeconds", { type: "Number",
        description: "deployment/settings.toml: fire risk weather max age seconds." }),
      FIRE_RISK_WEATHER_MAX_DISTANCE_KM: new CfnParameter(this, "FireRiskWeatherMaxDistanceKm", { type: "Number",
        description: "deployment/settings.toml: fire risk weather max distance km." }),
      FIRE_RISK_SURFACE_FUEL_MODEL: new CfnParameter(this, "FireRiskSurfaceFuelModel", { type: "Number",
        description: "deployment/settings.toml: fire risk surface fuel model." }),
      FIRE_RISK_SURFACE_FIRST_MINUTES: new CfnParameter(this, "FireRiskSurfaceFirstMinutes", { type: "Number",
        description: "deployment/settings.toml: fire risk surface first minutes." }),
      FIRE_RISK_SURFACE_SECOND_MINUTES: new CfnParameter(this, "FireRiskSurfaceSecondMinutes", { type: "Number",
        description: "deployment/settings.toml: fire risk surface second minutes." }),
      FIRE_RISK_SURFACE_THIRD_MINUTES: new CfnParameter(this, "FireRiskSurfaceThirdMinutes", { type: "Number",
        description: "deployment/settings.toml: fire risk surface third minutes." }),
      FIRE_RISK_SURFACE_MOISTURE_PROFILE: new CfnParameter(this, "FireRiskSurfaceMoistureProfile", { type: "String",
        description: "deployment/settings.toml: fire risk surface moisture profile." }),
      FIRE_RISK_LANDSCAPE_REQUEST_MAX_KIB: new CfnParameter(this, "FireRiskLandscapeRequestMaxKib", { type: "Number",
        description: "deployment/settings.toml: fire risk landscape request max kib." }),
      FIRE_RISK_LANDSCAPE_CALCULATION_TIMEOUT_SECONDS: new CfnParameter(this, "FireRiskLandscapeCalculationTimeoutSeconds", { type: "Number",
        description: "deployment/settings.toml: fire risk landscape calculation timeout seconds." }),
      FIRE_RISK_LANDSCAPE_ACQUISITION_TIMEOUT_SECONDS: new CfnParameter(this, "FireRiskLandscapeAcquisitionTimeoutSeconds", { type: "Number",
        description: "deployment/settings.toml: fire risk landscape acquisition timeout seconds." }),
      FIRE_RISK_LANDSCAPE_SOURCE_MAX_MIB: new CfnParameter(this, "FireRiskLandscapeSourceMaxMib", { type: "Number",
        description: "deployment/settings.toml: fire risk landscape source max mib." }),
      FIRE_RISK_LANDSCAPE_MAX_CELLS: new CfnParameter(this, "FireRiskLandscapeMaxCells", { type: "Number",
        description: "deployment/settings.toml: fire risk landscape max cells." }),
      FIRE_RISK_LANDSCAPE_INITIAL_RADIUS_M: new CfnParameter(this, "FireRiskLandscapeInitialRadiusM", { type: "Number",
        description: "deployment/settings.toml: fire risk landscape initial radius m." }),
      FIRE_RISK_LANDSCAPE_DIRECTIONAL_RADIUS: new CfnParameter(this, "FireRiskLandscapeDirectionalRadius", { type: "Number",
        description: "deployment/settings.toml: fire risk landscape directional radius." }),
    };
    // The private firetruck service reads these (firetruck-simulator/config/settings.py, checked when it starts); of
    // them, only the run lifetime also reaches the API.
    const firetruckLimits = {
      FIRETRUCK_RUN_LIFETIME_MINUTES: firetruckRunLifetime,
      FIRETRUCK_RUNS_PER_TASK: new CfnParameter(this, "FiretruckRunsPerTask", { type: "Number",
        description: "capacity.firetruck_runs_per_task in deployment/settings.toml: firetruck runs one service task simulates at once." }),
      FIRETRUCK_EVIDENCE_EVENTS_MAX: new CfnParameter(this, "FiretruckEvidenceEventsMax", { type: "Number",
        description: "capacity.firetruck_evidence_events_max in deployment/settings.toml: latest timeline events a running firetruck evidence snapshot carries." }),
    };
    // Only the SUMO worker reads these (traffic_simulator.settings.WORKER_SETTINGS, checked when it starts): it alone
    // simulates policies, builds, caches and pre-builds road networks, and captures TomTom traffic for runs.
    const workerLimits = {
      OSM_SOURCE_MAX_ATTEMPTS: new CfnParameter(this, "OsmSourceMaxAttempts", { type: "Number",
        description: "deployment/settings.toml: osm source max attempts." }),
      OSM_SOURCE_RETRY_MAX_SECONDS: new CfnParameter(this, "OsmSourceRetryMaxSeconds", { type: "Number",
        description: "deployment/settings.toml: osm source retry max seconds." }),
      POLICY_PARALLELISM: new CfnParameter(this, "PolicyParallelism", { type: "Number",
        description: "simulation.policy_parallelism in deployment/settings.toml: scenario policies one worker simulates at once." }),
      MAX_NETWORK_EDGES: new CfnParameter(this, "MaxNetworkEdges", { type: "Number",
        description: "simulation.max_network_edges in deployment/settings.toml: largest road network a run accepts, in edges." }),
      OVERPASS_RESPONSE_MAX_MB: new CfnParameter(this, "OverpassResponseMaxMb", { type: "Number",
        description: "simulation.overpass_response_max_mb in deployment/settings.toml: largest API or worker OpenStreetMap road download, in MB." }),
      CACHED_NETWORKS_PER_WORKER: new CfnParameter(this, "CachedNetworksPerWorker", { type: "Number",
        description: "cache.cached_networks_per_worker in deployment/settings.toml: road networks a worker keeps on its disk." }),
      CACHED_NETWORK_MIB_PER_WORKER: new CfnParameter(this, "CachedNetworkMibPerWorker", { type: "Number",
        description: "cache.cached_network_mib_per_worker in deployment/settings.toml: disk a worker's cached road networks may use, in MiB." }),
      DEMO_AREAS_TO_PREBUILD: new CfnParameter(this, "DemoAreasToPrebuild", { type: "String",
        description: 'cache.demo_areas_to_prebuild in deployment/settings.toml: comma-separated example areas a starting worker prepares ("" for none).' }),
      TOMTOM_REQUEST_CONCURRENCY: new CfnParameter(this, "TomtomRequestConcurrency", { type: "Number",
        description: "simulation.tomtom_request_concurrency in deployment/settings.toml: TomTom flow requests one traffic-speed run sends at once." }),
      OSM_SOURCE_HTTP_TIMEOUT_SECONDS: new CfnParameter(this, "OsmSourceHttpTimeoutSeconds", { type: "Number",
        description: "deployment/settings.toml: osm source http timeout seconds." }),
      ROAD_PREPARATION_TIMEOUT_SECONDS: new CfnParameter(this, "RoadPreparationTimeoutSeconds", { type: "Number",
        description: "deployment/settings.toml: road preparation timeout seconds." }),
      OSM_SOURCE_MAX_MIB: new CfnParameter(this, "OsmSourceMaxMib", { type: "Number",
        description: "deployment/settings.toml: osm source max mib." }),
      OSM_SOURCE_CACHE_MIB_PER_WORKER: new CfnParameter(this, "OsmSourceCacheMibPerWorker", { type: "Number",
        description: "deployment/settings.toml: osm source cache mib per worker." }),
      OSM_INDEX_CACHE_SECONDS: new CfnParameter(this, "OsmIndexCacheSeconds", { type: "Number",
        description: "deployment/settings.toml: osm index cache seconds." }),
      OSM_ROAD_XML_MAX_MB: new CfnParameter(this, "OsmRoadXmlMaxMb", { type: "Number",
        description: "deployment/settings.toml: osm road xml max mb." }),
    };
    // The artifacts bucket's lifecycle rule for the shared road-network cache (network-cache/); no task reads it.
    const networkCacheDays = new CfnParameter(this, "NetworkCacheDays", { type: "Number",
      description: "cache.network_cache_days in deployment/settings.toml: days the artifacts bucket keeps a shared road network." });
    const asEnvironment = (limits: Record<string, CfnParameter>) =>
      Object.fromEntries(Object.entries(limits).map(([name, parameter]) => [name, parameter.valueAsString]));
    // Task sizes come from deployment/settings.toml (capacity.<task>_vcpu and capacity.<task>_memory_gib), whole
    // vCPUs and GiB that the deployer has checked form a Fargate size. CloudFormation cannot multiply, so this table
    // turns them into the CPU units and MiB a task definition takes (n -> n * 1024).
    const fargateUnits = new CfnMapping(this, "FargateUnits", {
      mapping: Object.fromEntries(Array.from({ length: 120 }, (_, index) =>
        [String(index + 1), { Units: String((index + 1) * 1024) }])),
    });
    const taskSize = (vcpu: CfnParameter, memory: CfnParameter) => ({
      cpu: Token.asNumber(fargateUnits.findInMap(vcpu.valueAsString, "Units")),
      memoryLimitMiB: Token.asNumber(fargateUnits.findInMap(memory.valueAsString, "Units")),
    });
    const taskSizes: Record<string, { cpu: number; memoryLimitMiB: number }> = {
      Api: taskSize(
        new CfnParameter(this, "ApiVcpu", { type: "Number",
          description: "capacity.api_vcpu in deployment/settings.toml: vCPUs of the workspace API task." }),
        new CfnParameter(this, "ApiMemoryGib", { type: "Number",
          description: "capacity.api_memory_gib in deployment/settings.toml: memory of the workspace API task in GiB." })),
      Worker: taskSize(
        new CfnParameter(this, "WorkerVcpu", { type: "Number",
          description: "capacity.worker_vcpu in deployment/settings.toml: vCPUs of the SUMO worker task." }),
        new CfnParameter(this, "WorkerMemoryGib", { type: "Number",
          description: "capacity.worker_memory_gib in deployment/settings.toml: memory of the SUMO worker task in GiB." })),
      Firetruck: taskSize(
        new CfnParameter(this, "FiretruckVcpu", { type: "Number",
          description: "capacity.firetruck_vcpu in deployment/settings.toml: vCPUs of the private firetruck task." }),
        new CfnParameter(this, "FiretruckMemoryGib", { type: "Number",
          description: "capacity.firetruck_memory_gib in deployment/settings.toml: memory of the private firetruck task in GiB." })),
      FireRisk: taskSize(
        new CfnParameter(this, "FireRiskVcpu", { type: "Number",
          description: "capacity.fire_risk_vcpu in deployment/settings.toml: vCPUs of the private modeler." }),
        new CfnParameter(this, "FireRiskMemoryGib", { type: "Number",
          description: "capacity.fire_risk_memory_gib in deployment/settings.toml: memory of the private modeler." })),
      Radio: taskSize(
        new CfnParameter(this, "RadioVcpu", { type: "Number",
          description: "capacity.radio_vcpu in deployment/settings.toml: vCPUs of the private radio task." }),
        new CfnParameter(this, "RadioMemoryGib", { type: "Number",
          description: "capacity.radio_memory_gib in deployment/settings.toml: memory of the private radio task in GiB." })),
    };
    // [access] staging_allowed_cidrs from the installation file, split by IP version: comma-separated CIDR ranges,
    // or "" for none. Both are "" for a public installation; any range limits the API to the listed networks.
    const stagingIpv4 = new CfnParameter(this, "StagingAllowedIpv4Cidrs", {
      type: "String",
      description: '[access] staging_allowed_cidrs IPv4 ranges (comma-separated); "" when the installation is public.',
    });
    const stagingIpv6 = new CfnParameter(this, "StagingAllowedIpv6Cidrs", {
      type: "String",
      description: '[access] staging_allowed_cidrs IPv6 ranges (comma-separated); "" when the installation is public.',
    });
    const hasRuntime = new CfnCondition(this, "HasSimulationRuntime", {
      expression: Fn.conditionNot(Fn.conditionEquals(simulationRuntime.valueAsString, "")),
    });
    // The operator roles that may read and write this stack's bucket objects besides its own task roles (D52).
    const operators = operatorRoles(this);
    const vpc = new ec2.Vpc(this, "Network", {
      maxAzs: 2, natGateways: 1,
      subnetConfiguration: [
        { name: "public", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        { name: "app", subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 24 },
      ],
    });
    vpc.addGatewayEndpoint("S3Endpoint", { service: ec2.GatewayVpcEndpointAwsService.S3 });
    vpc.addGatewayEndpoint("DynamoEndpoint", { service: ec2.GatewayVpcEndpointAwsService.DYNAMODB });
    nag(vpc, "AwsSolutions-VPC7", "Accepted (D38): flow logs would add a log destination, a delivery role and " +
      "ongoing ingestion cost; the security groups already limit every task to HTTPS out and its named peers in.");
    const artifacts = new s3.Bucket(this, "Artifacts", {
      encryption: s3.BucketEncryption.S3_MANAGED, enforceSSL: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, removalPolicy: RemovalPolicy.RETAIN,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      // Versioning keeps an overwritten or deleted object recoverable; enabling it updates the bucket in place.
      versioned: true,
      // Completed bundles must outlive all retained children. Explicit removal/retention
      // tools can clean them later; a bucket-wide seven-day expiry broke reproducibility.
      // Only the shared road-network cache and firetruck evidence expire (lifecycle rules update the bucket in place);
      // the next run of an area rebuilds its network. Runs are frozen with their own copy, so nothing they need expires.
      lifecycleRules: [
        // Current sealed inputs follow metadata references; only superseded/deleted versions are disposable.
        { id: "ExpireNoncurrentRiskInputVersions", prefix: "fire-risk/inputs/",
          noncurrentVersionExpiration: Duration.days(1) },
        { id: "ExpireRegionalOsmSources", prefix: "osm-extracts/",
          expiration: Duration.days(networkCacheDays.valueAsNumber),
          noncurrentVersionExpiration: Duration.days(1) },
        { id: "ExpireSharedRoadNetworks", prefix: "network-cache/",
          expiration: Duration.days(networkCacheDays.valueAsNumber),
          // Housekeeping, not a setting: a replaced or expired network copy has no reader, so it goes after a day.
          noncurrentVersionExpiration: Duration.days(1) },
        // The firetruck service deletes each replaced evidence snapshot, so a run keeps one. A run's final snapshot
        // goes when no demo session or record can still point at it (capacity.demo_session_days), and so does any
        // snapshot a lost lease or a failed delete left behind.
        { id: "ExpireFiretruckEvidence", prefix: "firetruck/",
          expiration: Duration.days(simulatorLimits.DEMO_SESSION_DAYS.valueAsNumber),
          // Housekeeping, not a setting: a deleted or replaced snapshot has no reader, so its copy goes after a day.
          noncurrentVersionExpiration: Duration.days(1) },
        { id: "ExpireReplacedArtifacts",
          // Fixed retention, not a setting (D37): an overwritten or deleted run artifact or report stays recoverable
           // for 30 days, like BuildSource versions; reference-aware cleanup collects current orphan artifacts.
          noncurrentVersionExpiration: Duration.days(30),
          // Housekeeping: unfinished multipart uploads anywhere in the bucket, and delete markers left with no version.
          abortIncompleteMultipartUploadAfter: Duration.days(1), expiredObjectDeleteMarker: true },
      ],
    });
    nag(artifacts, "AwsSolutions-S1", s3AccessLogReason);
    const jobs = new dynamodb.Table(this, "Jobs", {
      partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST, timeToLiveAttribute: "expires",
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: dynamodb.TableEncryption.AWS_MANAGED, removalPolicy: RemovalPolicy.RETAIN,
      stream: dynamodb.StreamViewType.NEW_IMAGE,
    });
    jobs.addGlobalSecondaryIndex({
      indexName: "owner-created", partitionKey: { name: "owner", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "created", type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    jobs.addGlobalSecondaryIndex({
      indexName: "pending-outbox", partitionKey: { name: "pending_outbox", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "created", type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });
    const dlq = new sqs.Queue(this, "DeadLetters", {
      encryption: sqs.QueueEncryption.SQS_MANAGED, enforceSSL: true, retentionPeriod: Duration.days(14),
    });
    const queue = new sqs.Queue(this, "Runs", {
      encryption: sqs.QueueEncryption.SQS_MANAGED, enforceSSL: true,
      visibilityTimeout: Duration.seconds(180), receiveMessageWaitTime: Duration.seconds(20),
      retentionPeriod: Duration.days(4), deadLetterQueue: { queue: dlq, maxReceiveCount: 5 },
    });
    const fireRiskDeadLetters = new sqs.Queue(this, "FireRiskDeadLetters", {
      encryption: sqs.QueueEncryption.SQS_MANAGED, enforceSSL: true, retentionPeriod: Duration.days(14),
    });
    const fireRiskQueue = new sqs.Queue(this, "FireRiskPreparation", {
      encryption: sqs.QueueEncryption.SQS_MANAGED, enforceSSL: true,
      visibilityTimeout: Duration.seconds(180), receiveMessageWaitTime: Duration.seconds(20),
      retentionPeriod: Duration.days(4), deadLetterQueue: { queue: fireRiskDeadLetters, maxReceiveCount: 5 },
    });
    // The repair schedule invokes the dispatcher asynchronously; an invocation that still fails after Lambda's retries
    // lands here instead of being dropped (D9). The next minute's pending-index query finds the job again.
    const dispatcherDeadLetters = new sqs.Queue(this, "OutboxDispatcherDeadLetters", {
      encryption: sqs.QueueEncryption.SQS_MANAGED, enforceSSL: true,
      // Fixed (D37): SQS's longest retention, as for DeadLetters, so a failure can still be inspected.
      retentionPeriod: Duration.days(14),
    });
    // Every task and function logs to a group this stack owns, whose events expire after 30 days (D37, D53); the
    // group outlives its task or function, like every retained log group.
    const retainedLogs = (id: string) => new logs.LogGroup(this, id, {
      retention: logs.RetentionDays.ONE_MONTH, removalPolicy: RemovalPolicy.RETAIN,
    });
    const dispatcher = new lambda.Function(this, "RunOutboxDispatcher", {
      runtime: lambda.Runtime.PYTHON_3_14, handler: "index.handler", timeout: Duration.seconds(60),
      logGroup: retainedLogs("RunOutboxDispatcherLogs"),
      code: lambda.Code.fromInline(readFileSync(join(__dirname, "outbox_dispatcher.py"), "utf8")),
      environment: { JOBS_TABLE: jobs.tableName, QUEUE_URL: queue.queueUrl, FIRE_RISK_QUEUE_URL: fireRiskQueue.queueUrl },
      // Fixed (D38): one invocation for the table stream and one for the minute's repair query; a throttled
      // invocation is retried, and leases make a repeated delivery harmless.
      reservedConcurrentExecutions: 2, deadLetterQueue: dispatcherDeadLetters,
    });

    // Least privilege: outbox_dispatcher.py reads and clears the outbox flag on the base table and queries the sparse
    // pending index; the table's AWS managed aws/dynamodb key needs no KMS grant, and the stream event source below
    // adds its own stream read permissions.
    dispatcher.addToRolePolicy(new iam.PolicyStatement({
      actions: ["dynamodb:GetItem", "dynamodb:UpdateItem"], resources: [jobs.tableArn],
    }));
    dispatcher.addToRolePolicy(new iam.PolicyStatement({
      actions: ["dynamodb:Query"], resources: [`${jobs.tableArn}/index/pending-outbox`],
    }));
    queue.grantSendMessages(dispatcher);
    fireRiskQueue.grantSendMessages(dispatcher);
    dispatcher.addEventSource(new sources.DynamoEventSource(jobs, {
      startingPosition: lambda.StartingPosition.TRIM_HORIZON, batchSize: 20,
      bisectBatchOnError: true, reportBatchItemFailures: true, retryAttempts: 3,
    }));
    new events.Rule(this, "OutboxRepair", {
      schedule: events.Schedule.rate(Duration.minutes(1)), targets: [new targets.LambdaFunction(dispatcher)],
    });
    const lambdaLogging = "AWSLambdaBasicExecutionRole is AWS's minimal Lambda logging policy (CreateLogGroup, " +
      "CreateLogStream, PutLogEvents); an inline copy would grant the same.";
    nag(dispatcher, "AwsSolutions-IAM4", lambdaLogging,
      ["Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"]);
    nag(dispatcher, "AwsSolutions-IAM5", noResourceLevel("dynamodb:ListStreams",
      "the stream reads themselves are limited to the jobs table's stream."), ["Resource::*"]);
    const collector = new lambda.Function(this, "ArtifactCollector", {
      runtime: lambda.Runtime.PYTHON_3_14, handler: "index.handler", timeout: Duration.seconds(120),
      logGroup: retainedLogs("ArtifactCollectorLogs"),
      code: lambda.Code.fromInline(readFileSync(join(__dirname, "artifact_cleanup.py"), "utf8")),
      environment: { JOBS_TABLE: jobs.tableName, ARTIFACTS_BUCKET: artifacts.bucketName },
      reservedConcurrentExecutions: 1, deadLetterQueue: dispatcherDeadLetters,
    });
    collector.addToRolePolicy(new iam.PolicyStatement({
      actions: ["dynamodb:GetItem", "dynamodb:UpdateItem"], resources: [jobs.tableArn],
    }));
    collector.addToRolePolicy(new iam.PolicyStatement({
      actions: ["s3:ListBucket"], resources: [artifacts.bucketArn],
      conditions: { StringLike: { "s3:prefix":
        ["runs/", "runs/*", "reports/", "reports/*", "fire-risk/inputs/", "fire-risk/inputs/*"] } },
    }));
    collector.addToRolePolicy(new iam.PolicyStatement({
      actions: ["s3:DeleteObject"], resources: [artifacts.arnForObjects("runs/*"), artifacts.arnForObjects("reports/*"),
        artifacts.arnForObjects("fire-risk/inputs/*")],
    }));
    new events.Rule(this, "ArtifactCollection", {
      schedule: events.Schedule.rate(Duration.hours(1)), targets: [new targets.LambdaFunction(collector)],
    });
    nag(collector, "AwsSolutions-IAM4", lambdaLogging,
      ["Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"]);
    nag(collector, "AwsSolutions-IAM5", "Collection only deletes unreferenced run, report and risk input artifacts.",
      [{ regex: "/^Resource::<Artifacts[0-9A-F]+\\.Arn>\\/(runs|reports|fire-risk\\/inputs)\\/\\*$/g" }]);
    const contextSecret = new secretsmanager.Secret(this, "SimulationContextSecret", {
      description: "Service-only simulation context signing key shared with the FireGUard runtime and adapter.",
      generateSecretString: { passwordLength: 64, excludePunctuation: true },
    });
    const radioCredential = new secretsmanager.Secret(this, "RadioBrokerCredential", {
      description: "Server-only Broadcastify connection for the public workspace.",
      generateSecretString: { secretStringTemplate: JSON.stringify({ WS_URL: "", API_KEY: "" }),
        generateStringKey: "nonce", excludePunctuation: true },
    });
    const credentials = new secretsmanager.Secret(this, "ProviderCredentials", {
      description: "Optional traffic and satellite provider keys; blank values disable those connectors.",
      generateSecretString: {
        secretStringTemplate: JSON.stringify({ TOMTOM_API_KEY: "", FIRMS_MAP_KEY: "" }),
        generateStringKey: "nonce", excludePunctuation: true,
      },
    });
    for (const secret of [radioCredential, credentials]) {
      nag(secret, "AwsSolutions-SMG4", "D8: third-party keys (TomTom, FIRMS, Broadcastify) cannot rotate " +
        "automatically; architecture/45-credential-rotation-runbook.md gives the manual rotation and revocation steps.");
    }
    nag(contextSecret, "AwsSolutions-SMG4", "D8: an internal signing key, rotated by hand with the runbook's internal " +
      "key procedure (architecture/45); both readers re-read it within 5 minutes, so no restart is needed, and " +
      "automatic rotation would add a rotation function and dual-key verification.");
    const cluster = new ecs.Cluster(this, "Cluster", {
      vpc, containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });
    const alb = new elb.ApplicationLoadBalancer(this, "ApiLoadBalancer", {
      vpc, internetFacing: false, vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      dropInvalidHeaderFields: true,
    });
    // Private VPC origin avoids a public HTTP origin and requires no custom domain.
    const listener = alb.addListener("Http", {
      port: 80, open: false,
      defaultAction: elb.ListenerAction.fixedResponse(404),
    });
    const prefixList = new custom.AwsCustomResource(this, "CloudFrontPrefixList", {
      onUpdate: { service: "EC2", action: "describeManagedPrefixLists",
        parameters: { Filters: [{ Name: "prefix-list-name",
          Values: ["com.amazonaws.global.cloudfront.origin-facing"] }] },
        physicalResourceId: custom.PhysicalResourceId.of(`cloudfront-prefix-${this.region}`) },
      policy: custom.AwsCustomResourcePolicy.fromSdkCalls({
        resources: custom.AwsCustomResourcePolicy.ANY_RESOURCE,
      }),
      // The lookup's provider function (CDK's AwsCustomResource singleton) logs here.
      logGroup: retainedLogs("CloudFrontPrefixListLogs"),
    });
    nag(prefixList, "AwsSolutions-IAM5", noResourceLevel("ec2:DescribeManagedPrefixLists",
      "the lookup only reads the AWS-managed CloudFront origin-facing prefix list."), ["Resource::*"]);
    // The lookup's provider function is CDK's shared AwsCustomResource singleton; its role is the grant principal.
    nag(prefixList.grantPrincipal as iam.Role, "AwsSolutions-IAM4", lambdaLogging,
      ["Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"]);
    nag(alb, "AwsSolutions-ELB2", "Accepted (D38, G45): access logs would need a log bucket with an ELB delivery " +
      "policy; the internal ALB is reached only through CloudFront and the Gateway adapter, and the API logs every " +
      "request it serves.");
    alb.connections.allowFrom(ec2.Peer.prefixList(prefixList.getResponseField("PrefixLists.0.PrefixListId")),
      ec2.Port.tcp(80), "CloudFront managed origin addresses only");
    const gatewaySecurityGroup = new ec2.SecurityGroup(this, "SimulationGatewayAccess", {
      vpc, description: "FireGUard simulator Gateway adapter; private ALB access only",
      allowAllOutbound: false,
    });
    gatewaySecurityGroup.addEgressRule(alb.connections.securityGroups[0], ec2.Port.tcp(80));
    // The adapter calls only the load balancer: it holds no signing key and calls no AWS API (G17). That rule is a
    // separate resource, so an inline rule matching no traffic (CDK's own placeholder) keeps EC2's default allow-all
    // egress rule off this group.
    (gatewaySecurityGroup.node.defaultChild as ec2.CfnSecurityGroup).addPropertyOverride("SecurityGroupEgress", [{
      CidrIp: "255.255.255.255/32", Description: "Disallow all traffic", IpProtocol: "icmp", FromPort: 252, ToPort: 86,
    }]);
    alb.connections.allowFrom(gatewaySecurityGroup, ec2.Port.tcp(80), "Simulation Gateway adapter");
    const workspaceProxy = new cloudfront.Function(this, "WorkspaceApiPath", {
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      code: cloudfront.FunctionCode.fromInline(
        'function handler(e) { var p=e.request.uri.replace(/^\\/simulation-api/,"/api"); if(p.indexOf("/api/v1/")!==0) return {statusCode:404,statusDescription:"Not Found"}; e.request.uri=p; e.request.headers["x-lab-workspace"]={value:"public-demo"}; e.request.headers["x-lab-viewer-ip"]={value:e.viewer.ip}; return e.request; }'),
    });
    const workspaceAssets = new s3.Bucket(this, "WorkspaceAssets", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, enforceSSL: true,
      encryption: s3.BucketEncryption.S3_MANAGED, versioned: true, removalPolicy: RemovalPolicy.RETAIN,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
    });
    const integrated = new cloudfront.Distribution(this, "IntegratedWorkspace", {
      defaultRootObject: "index.html",
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(workspaceAssets),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        responseHeadersPolicy: cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
      },
      additionalBehaviors: {
        "simulation-api/*": {
          origin: origins.VpcOrigin.withApplicationLoadBalancer(alb, {
            httpPort: 80, protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
            readTimeout: Duration.seconds(60),
          }),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
          responseHeadersPolicy: cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
          functionAssociations: [{ function: workspaceProxy, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST }],
        },
      },
      comment: "FireGUard UI and authenticated simulation broker on one origin",
    });
    integrated.node.addDependency(listener.node.defaultChild!);
    // Visitors read the site through CloudFront's origin access control, an AWS service; the deployer publishes it.
    denyObjectAccessOutsideInstallation(workspaceAssets, [], operators);
    nag(workspaceAssets, "AwsSolutions-S1", s3AccessLogReason);
    nag(integrated, "AwsSolutions-CFR1", "A public demo with no geographic audience restriction.");
    nag(integrated, "AwsSolutions-CFR2", "API requests reach the load balancer only through its regional web ACL " +
      "(ApiWaf: managed rule groups, per-IP rate limits, staging allowlist); a CloudFront-scope web ACL would be " +
      "a second web ACL, with its own monthly charge, for the same traffic (D38). The UI is static files from a " +
      "private bucket.");
    nag(integrated, "AwsSolutions-CFR3", "Accepted (D38, G45): standard logs would need a log bucket with ACLs enabled or " +
      "us-east-1 log delivery resources; WAF metrics and sampled requests, the API's access and security event " +
      "logs, and CloudTrail are the records.");
    nag(integrated, "AwsSolutions-CFR4", "D4: there is no custom domain, so the distribution uses CloudFront's default " +
      "certificate, whose minimum viewer TLS version CloudFront sets; choosing one needs a custom certificate.");
    const workspaceUrl = `https://${integrated.distributionDomainName}`;
    // Legacy, unused: the application has no user sign-in, and no task reads this pool or its client.
    // They stay unchanged (pool RETAIN) so an in-place update never deletes them; an operator can remove
    // them by hand later. The backend AgentCore Gateway machine-to-machine client is separate and in use.
    const pool = new cognito.UserPool(this, "Researchers", {
      selfSignUpEnabled: false, signInAliases: { email: true }, autoVerify: { email: true },
      passwordPolicy: { minLength: 14, requireLowercase: true, requireUppercase: true,
        requireDigits: true, requireSymbols: true },
      mfa: cognito.Mfa.OPTIONAL, mfaSecondFactor: { sms: false, otp: true },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY, removalPolicy: RemovalPolicy.RETAIN,
    });
    const unusedPool = "Legacy and unused: the application has no sign-in (D20), and the pool is kept only so an " +
      "in-place update never deletes it; ";
    nag(pool, "AwsSolutions-COG2", unusedPool + "no client signs anyone in, so a required MFA would protect nothing.");
    nag(pool, "AwsSolutions-COG8", unusedPool + "the Plus feature plan bills per active user to protect sign-ins, " +
      "and there are none.");
    pool.addDomain("Domain", {
      cognitoDomain: { domainPrefix: props.prefix ? `${props.prefix}-lab` : `wildfire-lab-${this.account}-${this.region}` },
    });
    pool.addClient("WebClient", {
      generateSecret: false, preventUserExistenceErrors: true,
      oAuth: { flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
        callbackUrls: [`${workspaceUrl}/`], logoutUrls: [`${workspaceUrl}/`] },
      accessTokenValidity: Duration.hours(1), idTokenValidity: Duration.hours(1),
      enableTokenRevocation: true,
    });
    // Native endpoints keep DEFAULT activation from changing visitor traffic. CloudFormation creates
    // both before the API's binding is resolved, and exposes the actual selected live version.
    const runtimeEndpoints = endpointVersions.map((version, index) => {
      const endpoint = new agentcore.CfnRuntimeEndpoint(this, `RuntimeEndpoint${index === 0 ? "A" : "B"}`, {
        agentRuntimeId: Fn.select(1, Fn.split("/", simulationRuntime.valueAsString)),
        agentRuntimeVersion: version.valueAsString,
        name: index === 0 ? "RELEASE_A" : "RELEASE_B",
      });
      endpoint.cfnOptions.condition = hasRuntime;
      return endpoint;
    });
    const endpointIsA = new CfnCondition(this, "RuntimeEndpointIsA", {
      expression: Fn.conditionEquals(runtimeEndpoint.valueAsString, "RELEASE_A"),
    });
    const selectedRuntimeVersion = Fn.conditionIf(hasRuntime.logicalId,
      Fn.conditionIf(endpointIsA.logicalId, runtimeEndpoints[0].attrLiveVersion,
        runtimeEndpoints[1].attrLiveVersion), "").toString();
    const environment = {
      STORAGE_MODE: "aws", DATA_DIR: "/tmp/wildfire", AWS_REGION: this.region,
      ARTIFACT_BUCKET: artifacts.bucketName, JOBS_TABLE: jobs.tableName, QUEUE_URL: queue.queueUrl,
      PYTHONUNBUFFERED: "1",
      FABRIC_PUBLIC_URL: workspaceUrl, SIMULATION_RUNTIME_ARN: simulationRuntime.valueAsString,
      PUBLIC_DEMO_MODE: publicDemo.valueAsString,
      GEOCODER_FUNCTION_ARN: geocoder.valueAsString,
      ...asEnvironment(simulatorLimits),
    };
    const importImageRepository = (id: string, parameter: string) => {
      const arn = new CfnParameter(this, parameter, {type: "String"}).valueAsString;
      // This installer creates ECR names without path components.
      return ecr.Repository.fromRepositoryAttributes(this, id, {
        repositoryArn: arn, repositoryName: Fn.select(1, Fn.split("/", arn)),
      });
    };
    // The API runs backend.app; the worker runs the SUMO traffic-simulator image.
    const apiImage = ecs.ContainerImage.fromEcrRepository(
      importImageRepository("ApplicationImageRepository", "ImageRepositoryArn"), props.imageTag);
    const workerImage = ecs.ContainerImage.fromEcrRepository(
      importImageRepository("TrafficSimulatorImageRepository", "TrafficSimulatorRepositoryArn"), props.imageTag);
    // cdk-nag on each task: its environment holds only settings, and its roles' wildcards are the accepted ones.
    // writes: the role also writes or deletes objects, so the write grant's wildcards are accepted too; a read-only
    // role gets only the read grant's, and cdk-nag reports any write grant added to it later.
    const nagTask = (task: ecs.FargateTaskDefinition, prefixes: string, writes = true) => {
      nag(task, "AwsSolutions-ECS2", "Only non-secret settings are environment variables (deployment/settings.toml " +
        "limits, resource names and service URLs); every secret reaches the container from Secrets Manager as an " +
        "ECS secret, or for the API's signing key by ARN (D36).");
      nag(task.executionRole!, "AwsSolutions-IAM5", noResourceLevel("ecr:GetAuthorizationToken",
        "the execution role pulls only this task's image repository."), ["Resource::*"]);
      if (prefixes) {
        nag(task.taskRole, "AwsSolutions-IAM5", s3GrantReason, [...s3GrantActions, ...(writes ? s3WriteActions : []),
          { regex: `/^Resource::<(Artifacts|RadioAudio)[0-9A-F]+\\.Arn>\\/(${prefixes})\\/\\*$/g` }]);
      }
    };
    const makeTask = (name: string, worker: boolean) => {
      const task = new ecs.FargateTaskDefinition(this, `${name}Task`, {
        ...taskSizes[name],
        ephemeralStorageGiB: worker ? 30 : 21,
        runtimePlatform: { cpuArchitecture: ecs.CpuArchitecture.X86_64,
          operatingSystemFamily: ecs.OperatingSystemFamily.LINUX },
      });
      task.addVolume({ name: "scratch" });
      const group = retainedLogs(`${name}Logs`);
      const linux = new ecs.LinuxParameters(this, `${name}Linux`, {initProcessEnabled: true});
      linux.dropCapabilities(ecs.Capability.ALL);
      const container = task.addContainer(name, {
        image: worker ? workerImage : apiImage, linuxParameters: linux,
        environment: worker ? { ...environment, ...asEnvironment(workerLimits) }
          : { ...environment, ...asEnvironment(apiLimits), ...asEnvironment(fireRiskLimits),
              OVERPASS_RESPONSE_MAX_MB: workerLimits.OVERPASS_RESPONSE_MAX_MB.valueAsString,
              WEATHER_FUNCTION_ARN: weather.valueAsString, FIRE_RISK_QUEUE_URL: fireRiskQueue.queueUrl },
        // The API task runs two uvicorn processes whatever capacity.api_vcpu says (a fixed process model, D38), so
        // capacity.discoveries_per_api_process applies to each of them.
        command: worker ? ["python", "-m", "traffic_simulator.worker"] :
          ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2",
            "--ws-max-size", "4096"],
        user: "10001", readonlyRootFilesystem: true, stopTimeout: Duration.seconds(120),
        logging: ecs.LogDrivers.awsLogs({ streamPrefix: name, logGroup: group }),
        // Per-task secrets: the SUMO worker reads only the provider keys, and the radio credentials stay with
        // the API. The API reads the context-signing key itself, by ARN (below).
        secrets: {
          TOMTOM_API_KEY: ecs.Secret.fromSecretsManager(credentials, "TOMTOM_API_KEY"),
          FIRMS_MAP_KEY: ecs.Secret.fromSecretsManager(credentials, "FIRMS_MAP_KEY"),
        },
        healthCheck: worker ? undefined : {
          command: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)\""],
          interval: Duration.seconds(30), timeout: Duration.seconds(5), retries: 3,
          startPeriod: Duration.seconds(30),
        },
      });
      container.addMountPoints({ sourceVolume: "scratch", containerPath: "/tmp", readOnly: false });
      jobs.grantReadWriteData(task.taskRole);
      if (worker) {
        artifacts.grantReadWrite(task.taskRole, "runs/*"); queue.grantConsumeMessages(task.taskRole);
        // The shared road-network cache: the worker reads and writes network-cache/ and never deletes there (the
        // lifecycle rule expires it); the API has no access to it.
        artifacts.grantRead(task.taskRole, "network-cache/*"); artifacts.grantPut(task.taskRole, "network-cache/*");
        artifacts.grantRead(task.taskRole, "osm-extracts/*"); artifacts.grantPut(task.taskRole, "osm-extracts/*");
      }
      else {
        artifacts.grantRead(task.taskRole, "runs/*"); queue.grantSendMessages(task.taskRole);
        artifacts.grantRead(task.taskRole, "fire-risk/inputs/*"); artifacts.grantPut(task.taskRole, "fire-risk/inputs/*");
        fireRiskQueue.grantConsumeMessages(task.taskRole); fireRiskQueue.grantSendMessages(task.taskRole);
      }
      // The API also writes reports/ (granted with the private services below).
      nagTask(task, worker ? "runs|network-cache|osm-extracts" : "runs|reports|fire-risk/inputs");
      nag(task.taskRole, "AwsSolutions-IAM5", "The jobs table's owner-created index: DynamoDB names an index only " +
        "by an ARN under its table, and CDK's table grant covers the table's indexes.",
      [{ regex: "/^Resource::<Jobs[0-9A-F]+\\.Arn>\\/index\\/\\*$/g" }]);
      return { task, container };
    };
    const api = makeTask("Api", false);
    api.container.addEnvironment("SIMULATION_RUNTIME_ENDPOINT", runtimeEndpoint.valueAsString);
    api.container.addEnvironment("SIMULATION_RUNTIME_VERSION", selectedRuntimeVersion);
    // The API signs and verifies service contexts with a key it reads by ARN and reads again every 5 minutes, so a
    // rotated key applies without a restart (D36); an ECS secret would stay fixed for the task's life.
    api.container.addEnvironment("SIMULATION_CONTEXT_SECRET_ARN", contextSecret.secretArn);
    contextSecret.grantRead(api.task.taskRole);
    // The camera broker (backend.camera) alone reads [camera]; the worker gets none of it.
    for (const [name, parameter] of Object.entries(camera)) api.container.addEnvironment(name, parameter.valueAsString);
    // The whole provider secret, not a JSON-key reference (D43): ECS refuses to start a task whose
    // referenced JSON key is missing, and DELPHIRE_API_KEY exists only once an installation configures the camera, so
    // the API reads only that field of PROVIDER_CREDENTIALS. The execution role already reads this secret.
    api.container.addSecret("PROVIDER_CREDENTIALS", ecs.Secret.fromSecretsManager(credentials));
    // Only the API invokes the shared geocoder; it also resolves the address a radio investigation reports.
    const geocoding = new iam.Policy(this, "ApiGeocoding", {
      statements: [new iam.PolicyStatement({
        actions: ["lambda:InvokeFunction"], resources: [geocoder.valueAsString],
      })],
    });
    (geocoding.node.defaultChild as iam.CfnPolicy).cfnOptions.condition = hasGeocoder;
    geocoding.attachToRole(api.task.taskRole);
    const runtimePolicy = new iam.Policy(this, "SimulationRuntimeInvocation", {
      statements: [new iam.PolicyStatement({
        actions: ["bedrock-agentcore:InvokeAgentRuntime"],
        resources: [simulationRuntime.valueAsString, ...runtimeEndpoints.map(e => e.attrAgentRuntimeEndpointArn)],
      })],
    });
    runtimePolicy.attachToRole(api.task.taskRole);
    (runtimePolicy.node.defaultChild as iam.CfnPolicy).cfnOptions.condition = hasRuntime;
    api.container.addPortMappings({ containerPort: 8000 });
    const apiService = new ecs.FargateService(this, "ApiService", {
      cluster, taskDefinition: api.task, desiredCount: 1,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }, assignPublicIp: false,
      circuitBreaker: { rollback: true }, minHealthyPercent: 100, maxHealthyPercent: 200,
      healthCheckGracePeriod: Duration.seconds(60),
    });
    // Egress (D7): each task sends only HTTPS, to AWS APIs (through the NAT and the S3 and DynamoDB gateway endpoints)
    // and to the HTTPS data providers; the API also reaches its two private services. Security groups never filter
    // DNS, NTP or the task metadata endpoint. Each service keeps its own group, and only the group's rules change.
    const restrictEgress = (service: ecs.FargateService, ...peers: [ecs.FargateService, number][]) =>
      (service.connections.securityGroups[0].node.defaultChild as ec2.CfnSecurityGroup).addPropertyOverride(
        "SecurityGroupEgress", [
          { CidrIp: "0.0.0.0/0", IpProtocol: "tcp", FromPort: 443, ToPort: 443,
            Description: "HTTPS to AWS APIs and data providers" },
          ...peers.map(([peer, port]) => ({
            DestinationSecurityGroupId: peer.connections.securityGroups[0].securityGroupId,
            IpProtocol: "tcp", FromPort: port, ToPort: port, Description: `Private service on port ${port}` })),
        ]);
    const weatherInvocation = new iam.Policy(this, "ApiWeatherInvocation", {
      statements: [new iam.PolicyStatement({
        actions: ["lambda:InvokeFunction"], resources: [weather.valueAsString],
      })],
    });
    (weatherInvocation.node.defaultChild as iam.CfnPolicy).cfnOptions.condition = hasWeather;
    weatherInvocation.attachToRole(api.task.taskRole);
    let firetruckTaskRole: iam.IRole;
    { // Private firetruck and radio services, reachable only from the API through Cloud Map.
      const privatePorts: [ecs.FargateService, number][] = [];
      const firetruckRepository = importImageRepository("FiretruckImageRepository", "FiretruckRepositoryArn");
      const radioRepository = importImageRepository("RadioImageRepository", "RadioRepositoryArn");
      const fireRiskRepository = importImageRepository("FireRiskImageRepository", "FireRiskRepositoryArn");
      const radioModel = new CfnParameter(this, "RadioModelId", {
        type: "String", description: "Bedrock model or inference profile available to this account and region.",
      });
      const audioFiles = new CfnParameter(this, "RadioAudioKeys", {
        type: "String", default: "", description: "Comma-separated immutable input object keys in RadioAudioBucket.",
      });
      const namespace = `${props.prefix || "wildfire-lab"}.internal`;
      cluster.addDefaultCloudMapNamespace({name: namespace});
      const firetruckKey = new secretsmanager.Secret(this, "FiretruckServiceKey", {
        generateSecretString: {passwordLength: 64, excludePunctuation: true},
      });
      const radioKey = new secretsmanager.Secret(this, "RadioServiceKey", {
        generateSecretString: {passwordLength: 64, excludePunctuation: true},
      });
      const fireRiskKey = new secretsmanager.Secret(this, "FireRiskServiceKey", {
        generateSecretString: {passwordLength: 64, excludePunctuation: true},
      });
      for (const key of [firetruckKey, radioKey, fireRiskKey]) {
        nag(key, "AwsSolutions-SMG4", "D8: an internal service key between the API and one private service, " +
          "rotated by hand with the runbook's internal key procedure (architecture/45); automatic rotation would add " +
          "a rotation function and dual-key verification.");
      }
      const truckTable = new dynamodb.Table(this, "FiretruckEvidenceIndex", {
        partitionKey: {name: "id", type: dynamodb.AttributeType.STRING},
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST, removalPolicy: RemovalPolicy.RETAIN,
        pointInTimeRecoverySpecification: {pointInTimeRecoveryEnabled: true},
      });
      const radioAudio = new s3.Bucket(this, "RadioAudio", {
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, enforceSSL: true,
        encryption: s3.BucketEncryption.S3_MANAGED, versioned: true, removalPolicy: RemovalPolicy.RETAIN,
        objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      });
      const privateService = (name: string, repository: ecr.IRepository, port: number,
        serviceEnvironment: Record<string, string>, serviceSecrets: Record<string, ecs.Secret>) => {
        const task = new ecs.FargateTaskDefinition(this, `${name}PrivateTask`, taskSizes[name]);
        task.addVolume({name: "scratch"});
        const linux = new ecs.LinuxParameters(this, `${name}PrivateLinux`, {initProcessEnabled: true});
        linux.dropCapabilities(ecs.Capability.ALL);
        const container = task.addContainer(name, {
          linuxParameters: linux,
          image: ecs.ContainerImage.fromEcrRepository(repository, props.imageTag),
          environment: {AWS_REGION: this.region, PYTHONDONTWRITEBYTECODE: "1", ...serviceEnvironment},
          secrets: serviceSecrets, readonlyRootFilesystem: true, stopTimeout: Duration.seconds(120),
          logging: ecs.LogDrivers.awsLogs({streamPrefix: name, logGroup: retainedLogs(`${name}PrivateLogs`)}),
          healthCheck: {
            command: ["CMD-SHELL", `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${port}/health', timeout=3)"`],
            interval: Duration.seconds(30), timeout: Duration.seconds(5), retries: 3, startPeriod: Duration.seconds(60),
          },
        });
        container.addMountPoints({sourceVolume: "scratch", containerPath: "/tmp", readOnly: false});
        container.addPortMappings({containerPort: port});
        const service = new ecs.FargateService(this, `${name}PrivateService`, {
          cluster, taskDefinition: task, desiredCount: 1, assignPublicIp: false,
          vpcSubnets: {subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS},
          cloudMapOptions: {name: name.toLowerCase(), dnsTtl: Duration.seconds(10)},
          circuitBreaker: {rollback: true}, minHealthyPercent: 100, maxHealthyPercent: 200,
        });
        service.connections.allowFrom(apiService, ec2.Port.tcp(port), "Private broker only");
        restrictEgress(service);
        privatePorts.push([service, port]);
        new CfnOutput(this, `${name}ServiceName`, {value: service.serviceName});
        return task;
      };
      const fireRisk = privateService("FireRisk", fireRiskRepository, 8085, {
        FIRE_RISK_ENVIRONMENT: "ecs", ...asEnvironment(Object.fromEntries(
          ["INNER_KM", "MIDDLE_KM", "OUTER_KM", "RING_POINTS", "WIND_SHAPE_SCALE", "MAX_WIND_STRETCH",
            "REQUEST_BODY_MAX_KIB", "RESPONSE_MAX_KIB", "PERIMETER_MAX_VERTICES",
            "CALCULATIONS_PER_PROCESS", "CALCULATION_TIMEOUT_SECONDS", "LANDSCAPE_REQUEST_MAX_KIB",
            "LANDSCAPE_CALCULATION_TIMEOUT_SECONDS", "LANDSCAPE_MAX_CELLS", "LANDSCAPE_DIRECTIONAL_RADIUS"].map(name =>
              ["FIRE_RISK_"+name, fireRiskLimits[("FIRE_RISK_"+name) as keyof typeof fireRiskLimits]]))),
      }, {FIRE_RISK_SERVICE_KEY: ecs.Secret.fromSecretsManager(fireRiskKey)});
      nagTask(fireRisk, "", false);
      api.container.addEnvironment("FIRE_RISK_SERVICE_URL", `http://firerisk.${namespace}:8085`);
      api.container.addSecret("FIRE_RISK_SERVICE_KEY", ecs.Secret.fromSecretsManager(fireRiskKey));
      const truck = privateService("Firetruck", firetruckRepository, 8080, {
        ENVIRONMENT: "ecs", FIRETRUCK_TABLE: truckTable.tableName, FIRETRUCK_BUCKET: artifacts.bucketName,
        ...asEnvironment(firetruckLimits),
      }, {FIRETRUCK_SERVICE_KEY: ecs.Secret.fromSecretsManager(firetruckKey)});
      truckTable.grantReadWriteData(truck.taskRole);
      artifacts.grantReadWrite(truck.taskRole, "firetruck/*");
      firetruckTaskRole = truck.taskRole;
      // After the grants: a suppression reaches only the policy resources that exist when it is added.
      nagTask(truck, "firetruck");
      // The deployed investigation (monitor/private.py) reads only these settings and input/; it writes and geocodes nothing.
      const radio = privateService("Radio", radioRepository, 8000, {
        S3_BUCKET: radioAudio.bucketName, MONITOR_TEST_FILES: audioFiles.valueAsString,
        MONITOR_BEDROCK_MODEL_ID: radioModel.valueAsString,
        // No CORS_ORIGINS: only the API calls this private service, never a browser, so it allows no other origin.
        FIRE_KEYWORDS: "fire,wildfire,smoke,brush,vegetation,flames",
      }, {API_KEY: ecs.Secret.fromSecretsManager(radioKey)});
      radioAudio.grantRead(radio.taskRole, "input/*");
      nagTask(radio, "input", false);
      // The radio task reads the recordings and signs their playback URLs; the deployer copies them in.
      denyObjectAccessOutsideInstallation(radioAudio, [radio.taskRole.roleArn], operators);
      // Transcribe streaming has no resource-level permissions, so the resource stays "*"; the radio service streams
      // only in this stack's Region (its AWS_REGION).
      radio.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: ["transcribe:StartStreamTranscription"], resources: ["*"],
        conditions: { StringEquals: { "aws:RequestedRegion": this.region } },
      }));
      // Bedrock: only the radio model (RadioModelId, the installation's agent model), an inference profile or a
      // foundation model, given by ID or ARN. The part after the last delimiter, or the whole value when it has none:
      // splitting value + delimiter + value puts that part second either way.
      const after = (delimiter: string, value: string) =>
        Fn.select(1, Fn.split(delimiter, Fn.join("", [value, delimiter, value])));
      const radioModelName = after(":foundation-model/", after(":inference-profile/", radioModel.valueAsString));
      const bedrockArn = (region: string, resource: string) =>
        `arn:${this.partition}:bedrock:${region}:${resource}`;
      const radioProfile = bedrockArn(this.region, `${this.account}:inference-profile/${radioModelName}`);
      // The keyword detector calls InvokeModel only; no deployed radio code streams a model response.
      const invokeModel = ["bedrock:InvokeModel"];
      radio.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: invokeModel,
        resources: [radioProfile, bedrockArn(this.region, `:foundation-model/${radioModelName}`)],
      }));
      // A cross-Region inference profile runs each request on its model in another Region (a global profile names no
      // Region), so foundation models are allowed in any Region, but only when invoked through the radio profile.
      radio.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: invokeModel,
        resources: [bedrockArn("*", ":foundation-model/*"), bedrockArn("", ":foundation-model/*")],
        conditions: { StringEquals: { "bedrock:InferenceProfileArn": radioProfile } },
      }));
      nag(radio.taskRole, "AwsSolutions-IAM5", noResourceLevel("transcribe:StartStreamTranscription",
        "the statement is limited to this stack's Region (G71)."), ["Resource::*"]);
      nag(radio.taskRole, "AwsSolutions-IAM5", "A cross-Region inference profile runs the radio model in other " +
        "Regions (a global profile in none), so these foundation-model ARNs name every Region; the statement allows " +
        "them only through the radio model's own profile (condition bedrock:InferenceProfileArn, G71).", [
        "Resource::arn:<AWS::Partition>:bedrock:*::foundation-model/*",
        "Resource::arn:<AWS::Partition>:bedrock:::foundation-model/*"]);
      nag(radioAudio, "AwsSolutions-S1", s3AccessLogReason);
      api.container.addEnvironment("FIRETRUCK_SERVICE_URL", `http://firetruck.${namespace}:8080`);
      api.container.addEnvironment("RADIO_SERVICE_URL", `http://radio.${namespace}:8000`);
      api.container.addSecret("FIRETRUCK_SERVICE_KEY", ecs.Secret.fromSecretsManager(firetruckKey));
      // Replaces the legacy radio key injection. No public service URL is used.
      api.container.addSecret("BROADCASTIFY_PRIVATE_KEY", ecs.Secret.fromSecretsManager(radioKey));
      artifacts.grantReadWrite(api.task.taskRole, "reports/*");
      restrictEgress(apiService, ...privatePorts);
      new CfnOutput(this, "RadioAudioBucket", {value: radioAudio.bucketName});
      new CfnOutput(this, "FiretruckTable", {value: truckTable.tableName});
    }
    listener.addTargets("Api", {
      port: 8000, protocol: elb.ApplicationProtocol.HTTP, targets: [apiService],
      priority: 1, conditions: [elb.ListenerCondition.pathPatterns(["/*"])],
      healthCheck: { path: "/health", healthyHttpCodes: "200" },
      deregistrationDelay: Duration.seconds(30),
    });
    const worker = makeTask("Worker", true);
    // The API, the worker, the firetruck service and the hourly collector are the artifacts' only readers and writers.
    denyObjectAccessOutsideInstallation(artifacts, [api.task.taskRole.roleArn, worker.task.taskRole.roleArn,
      firetruckTaskRole.roleArn, collector.role!.roleArn], operators);
    const workerTasks = new CfnParameter(this, "WorkerTasks", {
      type: "Number", minValue: 1, maxValue: 5,
      description: "capacity.worker_tasks in deployment/settings.toml: independent concurrent SUMO consumers.",
    });
    const workerService = new ecs.FargateService(this, "WorkerService", {
      cluster, taskDefinition: worker.task, desiredCount: workerTasks.valueAsNumber,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }, assignPublicIp: false,
      circuitBreaker: { rollback: true }, minHealthyPercent: 100, maxHealthyPercent: 200,
    });
    restrictEgress(workerService);
    // Finish consumer rollout before the API can enqueue a newly supported job type.
    (apiService.node.defaultChild as ecs.CfnService).addResourceDependency(
      workerService.node.defaultChild as ecs.CfnService,
    );
    // Independent processes isolate SUMO and scratch caches; conditional leases fence duplicate SQS deliveries.
    new cloudwatch.Alarm(this, "DeadLetterAlarm", {
      metric: dlq.metricApproximateNumberOfMessagesVisible(), threshold: 1, evaluationPeriods: 1,
      alarmDescription: "A run exceeded retries. Inspect logs and its DynamoDB job state before redrive.",
    });
    new cloudwatch.Alarm(this, "ApiErrors", {
      metric: alb.metrics.httpCodeTarget(elb.HttpCodeTarget.TARGET_5XX_COUNT),
      threshold: 5, evaluationPeriods: 2, treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    // Staging gate (D20): an installation with [access] staging_allowed_cidrs gets one IP set per listed address
    // version, and WAF blocks every viewer outside them. A public installation passes "" for both and has neither.
    const hasStagingIpv4 = new CfnCondition(this, "HasStagingIpv4", {
      expression: Fn.conditionNot(Fn.conditionEquals(stagingIpv4.valueAsString, "")),
    });
    const hasStagingIpv6 = new CfnCondition(this, "HasStagingIpv6", {
      expression: Fn.conditionNot(Fn.conditionEquals(stagingIpv6.valueAsString, "")),
    });
    const isStaging = new CfnCondition(this, "IsStaging", {
      expression: Fn.conditionOr(hasStagingIpv4, hasStagingIpv6),
    });
    const hasStagingIpv4AndIpv6 = new CfnCondition(this, "HasStagingIpv4AndIpv6", {
      expression: Fn.conditionAnd(hasStagingIpv4, hasStagingIpv6),
    });
    const allowlist = (id: string, version: string, cidrs: CfnParameter, condition: CfnCondition) => {
      const set = new waf.CfnIPSet(this, id, {
        scope: "REGIONAL", ipAddressVersion: version, addresses: Fn.split(",", cidrs.valueAsString),
        // WAF descriptions allow only letters, digits, spaces and + = : # @ / - , . (no brackets or underscores).
        description: "Viewer networks allowed to use this staging installation, from its installation file.",
      });
      set.cfnOptions.condition = condition;
      return set;
    };
    const ipv4Allowlist = allowlist("StagingAllowlistIpv4", "IPV4", stagingIpv4, hasStagingIpv4);
    const ipv6Allowlist = allowlist("StagingAllowlistIpv6", "IPV6", stagingIpv6, hasStagingIpv6);
    // Raw CloudFormation (inside Fn::If). Only CloudFront requests carry X-Lab-Viewer-IP, which the WorkspaceApiPath
    // function overwrites with the viewer address; WAF skips the rule for a request without the header (the private
    // Gateway adapter, which needs a signed context), and an unparseable value counts as outside the allowlist.
    const inAllowlist = (set: waf.CfnIPSet) => ({ IPSetReferenceStatement: { Arn: set.attrArn,
      IPSetForwardedIPConfig: { HeaderName: "X-Lab-Viewer-IP", FallbackBehavior: "NO_MATCH", Position: "FIRST" } } });
    const stagingRule = Fn.conditionIf(isStaging.logicalId, {
      Name: "StagingAllowlist", Priority: 2,
      Action: { Block: { CustomResponse: { ResponseCode: 403, CustomResponseBodyKey: "staging-network-only",
        ResponseHeaders: [{ Name: "Cache-Control", Value: "no-store" }] } } },
      Statement: { NotStatement: { Statement: Fn.conditionIf(hasStagingIpv4AndIpv6.logicalId,
        { OrStatement: { Statements: [inAllowlist(ipv4Allowlist), inAllowlist(ipv6Allowlist)] } },
        Fn.conditionIf(hasStagingIpv4.logicalId, inAllowlist(ipv4Allowlist), inAllowlist(ipv6Allowlist))) } },
      VisibilityConfig: { CloudWatchMetricsEnabled: true, SampledRequestsEnabled: true,
        MetricName: "WildfireLabStagingAllowlist" },
    }, Aws.NO_VALUE);
    // Every rule and the default action keep sampled requests: WAF shows a sample of the last 3 hours of requests each
    // one matched, the edge's record of what it blocked (it stores no logs; CloudWatch keeps the per-rule counts).
    const acl = new waf.CfnWebACL(this, "ApiWaf", {
      scope: "REGIONAL", defaultAction: { allow: {} },
      visibilityConfig: { cloudWatchMetricsEnabled: true, sampledRequestsEnabled: true,
        metricName: "WildfireLabRequests" },
      customResponseBodies: {
        "rate-limited": wafErrorBody("RATE_LIMITED", "Too many requests that start work came from your network " +
          "in the last 5 minutes. Wait a few minutes, then try again.", true),
        "staging-network-only": wafErrorBody("NETWORK_NOT_ALLOWED",
          "This installation is in staging and accepts requests only from its approved networks.", false),
      },
      // WAF evaluates rules by priority, not array order: managed groups (0, 1), the staging allowlist (2), the
      // per-IP request rate (10) and the tighter per-IP rate for requests that start work (11).
      rules: [
        ...["AWSManagedRulesAmazonIpReputationList", "AWSManagedRulesKnownBadInputsRuleSet"].map((name, index) => ({
          name, priority: index, overrideAction: { none: {} },
          statement: { managedRuleGroupStatement: { vendorName: "AWS", name } },
          visibilityConfig: { cloudWatchMetricsEnabled: true, sampledRequestsEnabled: true, metricName: name },
        })),
        { name: "BoundRequestRate", priority: 10, action: { block: {} },
        statement: { rateBasedStatement: {
          limit: requestsPerIp.valueAsNumber, aggregateKeyType: "FORWARDED_IP",
          forwardedIpConfig: { headerName: "X-Lab-Viewer-IP", fallbackBehavior: "MATCH" },
        } }, visibilityConfig: { cloudWatchMetricsEnabled: true, sampledRequestsEnabled: true,
          metricName: "WildfireLabRateLimit" } },
        { name: "BoundWorkRate", priority: 11,
          action: { block: { customResponse: { responseCode: 429, customResponseBodyKey: "rate-limited",
            responseHeaders: [{ name: "Retry-After", value: String(rateWindowSeconds) },
              { name: "Cache-Control", value: "no-store" }] } } },
          statement: { rateBasedStatement: {
            limit: workRequestsPerIp.valueAsNumber, evaluationWindowSec: rateWindowSeconds,
            aggregateKeyType: "FORWARDED_IP",
            forwardedIpConfig: { headerName: "X-Lab-Viewer-IP", fallbackBehavior: "MATCH" },
            scopeDownStatement: { andStatement: { statements: [
              { byteMatchStatement: { fieldToMatch: { method: {} }, positionalConstraint: "EXACTLY",
                searchString: "post", textTransformations: [{ priority: 0, type: "LOWERCASE" }] } },
              { regexMatchStatement: { fieldToMatch: { uriPath: {} }, regexString: workStartingPaths,
                textTransformations: [{ priority: 0, type: "URL_DECODE" }, { priority: 1, type: "LOWERCASE" }] } },
            ] } },
          } }, visibilityConfig: { cloudWatchMetricsEnabled: true, sampledRequestsEnabled: true,
            metricName: "WildfireLabWorkRateLimit" } },
        stagingRule],
    });
    new waf.CfnWebACLAssociation(this, "ApiWafAssociation", {
      resourceArn: alb.loadBalancerArn, webAclArn: acl.attrArn,
    });
    new CfnOutput(this, "IntegratedWorkspaceUrl", { value: workspaceUrl });
    new CfnOutput(this, "WorkspaceAssetsBucket", { value: workspaceAssets.bucketName });
    new CfnOutput(this, "IntegratedDistributionId", { value: integrated.distributionId });
    new CfnOutput(this, "SimulationContextSecretArn", { value: contextSecret.secretArn });
    new CfnOutput(this, "RadioBrokerSecretArn", { value: radioCredential.secretArn });
    new CfnOutput(this, "SimulationInternalUrl", { value: `http://${alb.loadBalancerDnsName}` });
    new CfnOutput(this, "SimulationAdapterSecurityGroupId", { value: gatewaySecurityGroup.securityGroupId });
    new CfnOutput(this, "SimulationAdapterSubnetIds", { value: vpc.privateSubnets.map(s => s.subnetId).join(",") });
    new CfnOutput(this, "SimulationApiVersion", { value: integrationContract.api_version });
    new CfnOutput(this, "SimulationCapabilityVersion", { value: integrationContract.capability_version });
    // The legacy, unused researcher pool (see above), named so an operator can find it for manual removal.
    new CfnOutput(this, "UserPoolId", { value: pool.userPoolId });
    new CfnOutput(this, "ProviderSecretArn", { value: credentials.secretArn });
    new CfnOutput(this, "ClusterName", { value: cluster.clusterName });
    new CfnOutput(this, "WorkerServiceName", { value: workerService.serviceName });
    new CfnOutput(this, "ApiServiceName", { value: apiService.serviceName });
    new CfnOutput(this, "ArtifactsBucket", { value: artifacts.bucketName });
    new CfnOutput(this, "JobsTable", { value: jobs.tableName });
    new CfnOutput(this, "QueueUrl", { value: queue.queueUrl });
    new CfnOutput(this, "FireRiskQueueUrl", { value: fireRiskQueue.queueUrl });
    new CfnOutput(this, "FireRiskDeadLetterQueueUrl", { value: fireRiskDeadLetters.queueUrl });
    new CfnOutput(this, "DeadLetterQueueUrl", { value: dlq.queueUrl });
  }
}
