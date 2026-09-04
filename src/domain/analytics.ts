import { caseFold } from "unicode-case-folding";
import { roundDecimal, sequenceSimilarity } from "./scoring.ts";

export const COHORT_MIN_SAMPLE_SIZE = 6;
export const COHORT_MIN_CONFIDENCE = 0.7;

export interface CohortValue {
  userId: number;
  value: number;
  confidence: number;
}

export interface CohortBaseline {
  sampleSize: number;
  meanValue: number;
  stddevValue: number;
  medianValue: number;
  p90Value: number;
}

export interface CohortAnomaly {
  observedValue: number;
  baselineMean: number;
  zScore: number;
  direction: "above" | "below";
  confidence: number;
}

export interface TopicObservation {
  observationId: number;
  text: string;
  contextId: string;
  containerId: string;
  occurredAt: string;
}

export interface EmergingTopic {
  topicKey: string;
  topicKind: string;
  label: string;
  currentCount: number;
  baselineRate: number;
  velocity: number;
  contextCount: number;
  communityCount: number;
  unusualness: number;
  firstObservedAt: string;
  lastObservedAt: string;
  clusterTerms: readonly string[];
  crossCommunityDiffusion: boolean;
  evidence: readonly (readonly [number, string, string])[];
}

export interface RelationshipEdge {
  sourceUserId: number;
  targetUserId: number;
  strength: number;
  lastObservedAt: string;
}

export interface GraphMetric {
  userId: number;
  inDegree: number;
  outDegree: number;
  weightedDegree: number;
  betweenness: number;
  pagerank: number;
  clusterId: number;
  isBridge: boolean;
  influenceScore: number;
}

export interface IdentityAccount {
  accountId: number;
  platform: string;
  platformUserId: string;
  username: string;
  userId: number | null;
  context: string;
}

export interface IdentitySuggestion {
  leftPlatformAccountId: number;
  rightPlatformAccountId: number;
  confidence: number;
  usernameSimilarity: number;
  identifierSimilarity: number;
  sharedContext: boolean;
  manualApprovalRequired: true;
  modelVersion: 1;
}

export interface RelationshipOccurrence {
  sourceUserId: number;
  targetUserId: number;
  occurredAt: string;
}

const TOPIC_TOKEN_PATTERN = /[\p{L}\p{N}_'-]{3,}/gu;
const TOPIC_URL_PATTERN = /https?:\/\/[^\s<>]+/giu;
const TOPIC_STOP_WORDS = new Set([
  "the",
  "and",
  "that",
  "this",
  "with",
  "from",
  "have",
  "your",
  "you",
  "for",
  "are",
  "was",
  "not",
  "but",
  "they",
  "our",
  "will",
  "just",
  "into",
  "about",
  "http",
  "https",
  "www",
]);

export function calculateCohortBaseline(
  values: readonly number[],
): CohortBaseline {
  if (values.length === 0) {
    throw new TypeError("cohort values must not be empty");
  }
  const ordered = [...values].sort((left, right) => left - right);
  const mean = values.reduce((total, value) => total + value, 0) /
    values.length;
  const variance = values.reduce(
    (total, value) => total + (value - mean) ** 2,
    0,
  ) / values.length;
  const middle = Math.floor(ordered.length / 2);
  const median = ordered.length % 2 === 0
    ? (ordered[middle - 1] + ordered[middle]) / 2
    : ordered[middle];
  return Object.freeze({
    sampleSize: values.length,
    meanValue: mean,
    stddevValue: Math.sqrt(variance),
    medianValue: median,
    p90Value: ordered[
      Math.min(ordered.length - 1, Math.ceil(0.9 * ordered.length) - 1)
    ],
  });
}

export function calculatePeerAnomaly(
  values: readonly CohortValue[],
  userId: number,
): CohortAnomaly | null {
  const selected = values.find((item) => item.userId === userId);
  if (!selected || selected.confidence < COHORT_MIN_CONFIDENCE) return null;
  const peers = values.filter((item) => item.userId !== userId).map((item) =>
    item.value
  );
  if (peers.length < COHORT_MIN_SAMPLE_SIZE - 1) return null;
  return calculateBaselineAnomaly(
    selected.value,
    selected.confidence,
    calculateCohortBaseline(peers),
  );
}

export function calculateBaselineAnomaly(
  observedValue: number,
  confidence: number,
  baseline: CohortBaseline,
): CohortAnomaly | null {
  if (
    baseline.sampleSize < COHORT_MIN_SAMPLE_SIZE - 1 ||
    baseline.stddevValue === 0 ||
    confidence < COHORT_MIN_CONFIDENCE
  ) return null;
  const zScore = (observedValue - baseline.meanValue) / baseline.stddevValue;
  if (Math.abs(zScore) < 1.25) return null;
  return Object.freeze({
    observedValue,
    baselineMean: baseline.meanValue,
    zScore,
    direction: zScore > 0 ? "above" : "below",
    confidence,
  });
}

export function calculateEmergingTopics(
  observations: readonly TopicObservation[],
  communityId: number,
  now: string,
): readonly EmergingTopic[] {
  const currentStart = new Date(new Date(now).getTime() - 24 * 60 * 60 * 1000)
    .toISOString().replace(".000Z", "+00:00");
  const occurrences = new Map<
    string,
    Array<readonly [number, string, string, string, string]>
  >();
  for (const observation of observations) {
    const tokens = [...observation.text.matchAll(TOPIC_TOKEN_PATTERN)]
      .map((match) => match[0].toLocaleLowerCase())
      .filter((token) => !TOPIC_STOP_WORDS.has(token));
    const context = observation.containerId || observation.contextId || "";
    const seen = new Set(tokens.map((token) => `term:${token}`));
    for (let index = 0; index + 1 < tokens.length; index += 1) {
      seen.add(`phrase:${tokens[index]} ${tokens[index + 1]}`);
    }
    for (const match of observation.text.matchAll(TOPIC_URL_PATTERN)) {
      try {
        const hostname = new URL(match[0].replace(/[.,;!?)]+$/u, "")).hostname
          .toLocaleLowerCase().replace(/^www\./u, "");
        if (hostname) seen.add(`domain:${hostname}`);
      } catch {
        // Python's urlparse likewise contributes no domain for malformed URLs.
      }
    }
    for (const key of seen) {
      const separator = key.indexOf(":");
      const kind = key.slice(0, separator);
      const label = key.slice(separator + 1);
      const items = occurrences.get(key) ?? [];
      items.push([
        observation.observationId,
        context,
        observation.occurredAt,
        kind,
        label,
      ]);
      occurrences.set(key, items);
    }
  }

  const topics: EmergingTopic[] = [];
  for (const key of [...occurrences.keys()].sort()) {
    const items = occurrences.get(key)!;
    const recent = items.filter((item) => item[2] >= currentStart);
    if (recent.length === 0) continue;
    const baselineRate = (items.length - recent.length) / 7;
    const velocity = recent.length - baselineRate;
    const contexts = new Set(recent.map((item) => item[1]).filter(Boolean));
    const separator = key.indexOf(":");
    const kind = key.slice(0, separator);
    const label = key.slice(separator + 1);
    const minimum = kind === "domain" && contexts.size > 1 ? 1 : 2;
    if (recent.length < minimum) continue;
    const labelTerms = new Set(label.split(" "));
    const clusterTerms = kind === "phrase"
      ? [...occurrences.keys()].filter((item) => {
        if (!item.startsWith("phrase:")) return false;
        return item.slice(7).split(" ").some((term) => labelTerms.has(term));
      }).map((item) => item.slice(7)).sort().slice(0, 12)
      : [];
    topics.push(Object.freeze({
      topicKey: communityId === 1 ? key : `${communityId}:${key}`,
      topicKind: kind,
      label,
      currentCount: recent.length,
      baselineRate,
      velocity,
      contextCount: contexts.size,
      communityCount: contexts.size,
      unusualness: Math.max(0, velocity) * Math.log2(2 + contexts.size),
      firstObservedAt: items.map((item) => item[2]).sort()[0],
      lastObservedAt: items.map((item) => item[2]).sort().at(-1)!,
      clusterTerms: Object.freeze(clusterTerms),
      crossCommunityDiffusion: contexts.size > 1,
      evidence: Object.freeze(
        recent.slice(0, 25).map((item) =>
          Object.freeze([item[0], item[1], item[2]] as const)
        ),
      ),
    }));
  }
  return Object.freeze(topics);
}

export function calculateGraphMetrics(
  edges: readonly RelationshipEdge[],
  calculatedAt: string,
): readonly GraphMetric[] {
  const referenceTime = Date.parse(calculatedAt);
  if (!Number.isFinite(referenceTime)) {
    throw new TypeError("invalid calculated timestamp");
  }
  const nodes = [
    ...new Set(edges.flatMap((edge) => [edge.sourceUserId, edge.targetUserId])),
  ]
    .sort((left, right) => left - right);
  if (nodes.length === 0) return Object.freeze([]);
  const outgoing = numberMap(nodes);
  const incoming = numberMap(nodes);
  const undirected = setMap(nodes);
  const weightedNeighbors = numberMap(nodes);
  for (const edge of edges) {
    const observedTime = Date.parse(edge.lastObservedAt);
    const ageDays = Number.isFinite(observedTime)
      ? Math.max(0, (referenceTime - observedTime) / 86_400_000)
      : 0;
    const weight = edge.strength * Math.exp(-Math.log(2) * ageDays / 30);
    addWeight(outgoing.get(edge.sourceUserId)!, edge.targetUserId, weight);
    addWeight(incoming.get(edge.targetUserId)!, edge.sourceUserId, weight);
    undirected.get(edge.sourceUserId)!.add(edge.targetUserId);
    undirected.get(edge.targetUserId)!.add(edge.sourceUserId);
    addWeight(
      weightedNeighbors.get(edge.sourceUserId)!,
      edge.targetUserId,
      weight,
    );
    addWeight(
      weightedNeighbors.get(edge.targetUserId)!,
      edge.sourceUserId,
      weight,
    );
  }
  let pagerank = new Map(nodes.map((node) => [node, 1 / nodes.length]));
  for (let iteration = 0; iteration < 30; iteration += 1) {
    const nextRank = new Map(nodes.map((node) => [node, 0.15 / nodes.length]));
    const dangling = nodes.filter((node) => outgoing.get(node)!.size === 0)
      .reduce((total, node) => total + pagerank.get(node)!, 0);
    for (const node of nodes) {
      nextRank.set(node, nextRank.get(node)! + 0.85 * dangling / nodes.length);
    }
    for (const source of nodes) {
      const targets = [...outgoing.get(source)!.entries()].sort(numericEntry);
      const total = targets.reduce((sum, item) => sum + item[1], 0);
      for (const [target, weight] of targets) {
        nextRank.set(
          target,
          nextRank.get(target)! + 0.85 * pagerank.get(source)! * weight / total,
        );
      }
    }
    pagerank = nextRank;
  }
  const betweenness = calculateBetweenness(nodes, undirected);
  const bridges = calculateArticulationPoints(nodes, undirected);
  const clusters = calculateGraphClusters(nodes, weightedNeighbors);
  const weights = nodes.map((node) =>
    sumValues(outgoing.get(node)!) + sumValues(incoming.get(node)!)
  );
  const maxWeight = Math.max(...weights) || 1;
  return Object.freeze(nodes.map((node) => {
    const inDegree = sumValues(incoming.get(node)!);
    const outDegree = sumValues(outgoing.get(node)!);
    const weightedDegree = inDegree + outDegree;
    return Object.freeze({
      userId: node,
      inDegree,
      outDegree,
      weightedDegree,
      betweenness: betweenness.get(node)!,
      pagerank: pagerank.get(node)!,
      clusterId: clusters.get(node)!,
      isBridge: bridges.has(node),
      influenceScore: 0.65 * pagerank.get(node)! +
        0.35 * weightedDegree / maxWeight,
    });
  }));
}

export function calculateIdentitySuggestions(
  accounts: readonly IdentityAccount[],
  minimumConfidence = 0.55,
): readonly IdentitySuggestion[] {
  const suggestions: IdentitySuggestion[] = [];
  for (let leftIndex = 0; leftIndex < accounts.length; leftIndex += 1) {
    const left = accounts[leftIndex];
    for (const right of accounts.slice(leftIndex + 1)) {
      if (
        left.platform === right.platform ||
        (left.userId !== null && left.userId === right.userId)
      ) continue;
      const leftName = normalizeIdentityName(left.username);
      const rightName = normalizeIdentityName(right.username);
      const contextMatch = Boolean(
        left.context && right.context &&
          caseFold(left.context) === caseFold(right.context),
      );
      if (
        !contextMatch &&
        (!leftName || !rightName || leftName[0] !== rightName[0] ||
          Math.abs(leftName.length - rightName.length) > 4)
      ) continue;
      const nameScore = sequenceSimilarity(leftName, rightName);
      const identifierScore = sequenceSimilarity(
        normalizeIdentityName(left.platformUserId),
        normalizeIdentityName(right.platformUserId),
      );
      const confidence = Math.min(
        0.99,
        0.55 * nameScore + 0.30 * identifierScore +
          0.15 * Number(contextMatch),
      );
      if (confidence < minimumConfidence) continue;
      suggestions.push(Object.freeze({
        leftPlatformAccountId: left.accountId,
        rightPlatformAccountId: right.accountId,
        confidence,
        usernameSimilarity: roundDecimal(nameScore, 4),
        identifierSimilarity: roundDecimal(identifierScore, 4),
        sharedContext: contextMatch,
        manualApprovalRequired: true,
        modelVersion: 1,
      }));
    }
  }
  return Object.freeze(suggestions);
}

export function calculatePropagationPath(
  occurrences: readonly RelationshipOccurrence[],
  sourceUserId: number,
  targetUserId: number,
): readonly number[] {
  const adjacency = new Map<number, Array<readonly [number, string]>>();
  for (const occurrence of occurrences) {
    const targets = adjacency.get(occurrence.sourceUserId) ?? [];
    targets.push([occurrence.targetUserId, occurrence.occurredAt]);
    adjacency.set(occurrence.sourceUserId, targets);
  }
  const queue: Array<readonly [number, readonly number[], string]> = [
    [sourceUserId, [sourceUserId], ""],
  ];
  const bestTime = new Map([[sourceUserId, ""]]);
  while (queue.length > 0) {
    const [node, path, priorTime] = queue.shift()!;
    if (node === targetUserId) return Object.freeze([...path]);
    for (const [next, occurredAt] of adjacency.get(node) ?? []) {
      if (occurredAt < priorTime || path.includes(next)) continue;
      const existing = bestTime.get(next);
      if (existing !== undefined && existing <= occurredAt) continue;
      bestTime.set(next, occurredAt);
      queue.push([next, [...path, next], occurredAt]);
    }
  }
  return Object.freeze([]);
}

function normalizeIdentityName(value: string): string {
  return caseFold(value).replace(/[^a-z0-9]/gu, "");
}

function numberMap(nodes: readonly number[]): Map<number, Map<number, number>> {
  return new Map(nodes.map((node) => [node, new Map<number, number>()]));
}

function setMap(nodes: readonly number[]): Map<number, Set<number>> {
  return new Map(nodes.map((node) => [node, new Set<number>()]));
}

function addWeight(
  targets: Map<number, number>,
  target: number,
  weight: number,
): void {
  targets.set(target, (targets.get(target) ?? 0) + weight);
}

function sumValues(values: Map<number, number>): number {
  return [...values.values()].reduce((total, value) => total + value, 0);
}

function numericEntry(
  left: readonly [number, number],
  right: readonly [number, number],
): number {
  return left[0] - right[0];
}

function calculateGraphClusters(
  nodes: readonly number[],
  graph: Map<number, Map<number, number>>,
): Map<number, number> {
  const labels = new Map(nodes.map((node) => [node, node]));
  for (let iteration = 0; iteration < 25; iteration += 1) {
    let changed = false;
    for (const node of nodes) {
      const neighbors = [...graph.get(node)!.entries()].sort(numericEntry);
      if (neighbors.length === 0) continue;
      const scores = new Map<number, number>();
      for (const [neighbor, weight] of neighbors) {
        const label = labels.get(neighbor)!;
        scores.set(label, (scores.get(label) ?? 0) + weight);
      }
      const nextLabel = [...scores.entries()].sort((left, right) =>
        right[1] - left[1] || left[0] - right[0]
      )[0][0];
      if (labels.get(node) !== nextLabel) {
        labels.set(node, nextLabel);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const canonical = new Map(
    [...new Set(labels.values())].sort((left, right) => left - right)
      .map((label, index) => [label, index + 1]),
  );
  return new Map(
    nodes.map((node) => [node, canonical.get(labels.get(node)!)!]),
  );
}

function calculateBetweenness(
  nodes: readonly number[],
  graph: Map<number, Set<number>>,
): Map<number, number> {
  const centrality = new Map(nodes.map((node) => [node, 0]));
  for (const source of nodes) {
    const stack: number[] = [];
    const parents = new Map(nodes.map((node) => [node, [] as number[]]));
    const sigma = new Map(nodes.map((node) => [node, node === source ? 1 : 0]));
    const distance = new Map(
      nodes.map((node) => [node, node === source ? 0 : -1]),
    );
    const queue = [source];
    while (queue.length > 0) {
      const node = queue.shift()!;
      stack.push(node);
      for (
        const next of [...graph.get(node)!].sort((left, right) => left - right)
      ) {
        if (distance.get(next)! < 0) {
          queue.push(next);
          distance.set(next, distance.get(node)! + 1);
        }
        if (distance.get(next) === distance.get(node)! + 1) {
          sigma.set(next, sigma.get(next)! + sigma.get(node)!);
          parents.get(next)!.push(node);
        }
      }
    }
    const dependency = new Map(nodes.map((node) => [node, 0]));
    while (stack.length > 0) {
      const child = stack.pop()!;
      for (const parent of parents.get(child)!) {
        dependency.set(
          parent,
          dependency.get(parent)! + sigma.get(parent)! / sigma.get(child)! *
              (1 + dependency.get(child)!),
        );
      }
      if (child !== source) {
        centrality.set(child, centrality.get(child)! + dependency.get(child)!);
      }
    }
  }
  const scale = Math.max(1, (nodes.length - 1) * (nodes.length - 2));
  return new Map(nodes.map((node) => [node, centrality.get(node)! / scale]));
}

function calculateArticulationPoints(
  nodes: readonly number[],
  graph: Map<number, Set<number>>,
): Set<number> {
  let time = 0;
  const discovered = new Map<number, number>();
  const low = new Map<number, number>();
  const parent = new Map<number, number | null>();
  const result = new Set<number>();
  const visit = (node: number): void => {
    time += 1;
    discovered.set(node, time);
    low.set(node, time);
    let children = 0;
    for (
      const next of [...graph.get(node)!].sort((left, right) => left - right)
    ) {
      if (!discovered.has(next)) {
        parent.set(next, node);
        children += 1;
        visit(next);
        low.set(node, Math.min(low.get(node)!, low.get(next)!));
        if (parent.get(node) === null && children > 1) result.add(node);
        if (
          parent.get(node) !== null && low.get(next)! >= discovered.get(node)!
        ) {
          result.add(node);
        }
      } else if (next !== parent.get(node)) {
        low.set(node, Math.min(low.get(node)!, discovered.get(next)!));
      }
    }
  };
  for (const node of nodes) {
    if (!discovered.has(node)) {
      parent.set(node, null);
      visit(node);
    }
  }
  return result;
}
