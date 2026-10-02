"""Synthetic source files in the four native languages, with tricky cases.

Kept as Python strings on purpose: the repository never contains native source files
(see ``test_repo_hygiene``), and none of this code is upstream code. Line numbers in the
tests refer to these exact strings; ``_src`` strips only the first newline.
"""

from __future__ import annotations


def _src(text: str) -> str:
    return text[1:] if text.startswith("\n") else text


RUST = _src(
    r'''
//! Module docs mention fn not_a_function() {}
use crate::inputs::{CandidateScoringInputs, QueryScoringContext};
use std::collections::HashMap;

/* block comment /* nested */ with struct Hidden; */
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ValueScores<T: Into<Vec<Option<HashMap<String, u8>>>>> {
    pub weighted: Vec<f64>,
    pub raw: T,
}

pub(crate) enum Mode {
    Fast,
    Slow(u32),
}

pub trait Scorer: Send + Sync {
    fn score(&self, input: &CandidateScoringInputs) -> f64;
    fn name(&self) -> String {
        String::from("scorer")
    }
}

impl<'a, T: Clone + Iterator<Item = u32>> Scorer for crate::weights::Weighted<'a, T>
where
    T: Send + Sync,
{
    fn score(&self, input: &CandidateScoringInputs) -> f64 {
        let label = "fn fake_inside_string() { }";
        let brace = '{';
        let raw = r#"raw "quoted" fn fake_raw() {"#;
        self.weights.reply_weight_for(input) * helper::<f64>(2.0) + lookup(&label)
    }
}

impl Mode {
    pub const fn is_fast(&self) -> bool {
        matches!(self, Mode::Fast)
    }
}

pub const NEGATIVE_SCORES_OFFSET: f64 = 0.0;
static mut COUNTER: [u8; 4] = [0; 4];

param!(ReplyWeight, f64, "fixture_reply_weight", 5.0);
param!(
    ClickWeight,
    f64,
    "fixture_click_weight",
    0.3
);

macro_rules! weighted {
    ($score:expr, $weight:expr) => {
        $score * $weight
    };
}

pub fn compute_weighted_score(weights: &HashMap<String, f64>, scores: &[f64]) -> f64 {
    fn inner_apply(score: f64, weight: f64) -> f64 {
        weighted!(score, weight)
    }
    let total: f64 = scores.iter().map(|s| inner_apply(*s, 1.0)).sum();
    Box::new(total).clamp(0.0, 1.0)
}

#[cfg(test)]
mod tests {
    #[test]
    fn it_scores() {
        assert_eq!(super::compute_weighted_score(&Default::default(), &[]), 0.0);
    }
}
'''
)

SCALA = _src(
    r'''
package com.example.ranking

import com.example.util.{Helper => H, _}
import scala.concurrent.{ExecutionContext, Future}

/** Docs mention def notADef(x: Int) = x and class NotAClass */
@SerialVersionUID(1L)
final case class Candidate[T <: Ordered[T]](
  id: Long,
  val scores: Map[String, List[Double]],
  label: String)
    extends Scored[T]
    with Serializable {

  override val name: String = s"candidate-${id}"

  def total: Double = scores.values.flatten.sum

  def weighted(weights: Map[String, Double])(implicit ec: ExecutionContext): Double = {
    val parts = scores.map { case (key, values) => values.sum * weights.getOrElse(key, 0.0) }
    parts.sum
  }
}

sealed trait Shape
case object Empty extends Shape
case class Circle(radius: Double) extends Shape

object Registry extends BaseRegistry[String] {
  final val MaxResults = 100
  lazy val cache = new LruCache[String, Int](MaxResults)
  private[this] var counter = 0
  type Key = (String, Int)

  def build(
    config: Config,
    stats: StatsReceiver
  ): Registry = {
    val serializer = keySerializer(config.keyType)
    lookup(serializer) match {
      case Some(found) => found
      case None => fallback()
    }
  }

  def chained(x: Int): Int =
    compute(x)
      .map(_ + 1)
      .getOrElse(0)

  def describe(value: Int): String = s"value ${format(value, "x")} and ${"}"} done"

  object Nested {
    def +(other: Int): Int = other
    val query = """triple "quoted" def fake() = 1
      continues here"""
  }
}

class Plain(x: Int) {
  def this() = this(0)
}
'''
)

JAVA = _src(
    r'''
package com.example.ranking;

import java.util.List;
import java.util.Map;
import static java.util.Map.Entry;

/** Docs mention void notAMethod() {} and class NotAClass. */
@Deprecated
@SuppressWarnings({"unchecked", "rawtypes"})
public final class RankingService<T extends Comparable<T>> extends BaseService implements Runnable {
  private static final Map<String, List<Integer>> TABLE = new HashMap<>();
  public static final double CLICK_WEIGHT = 0.3;
  private final int limit;

  public RankingService(int limit) {
    super(limit);
    this.limit = limit;
  }

  @Override
  public void run() {
    Runnable task = new Runnable() {
      @Override
      public void run() {
        refresh(limit);
      }
    };
    List<String> ids = fetchIds(TABLE.get("a"), limit);
    String decoy = "void fakeMethod() {}";
    String block = """
        class FakeClass { void fake() {} }
        """;
  }

  public <K, V extends List<K>> Map<K, List<V>> group(Map<K, V> input) throws Exception {
    return transform(input, (key, value) -> value.size());
  }

  static class Inner {
    int[] values() { return new int[] {1, 2}; }
  }

  interface Shape {
    int SIDES = 4;
    double area();
    default String label() { return "shape"; }
  }

  enum Color {
    RED("r"), GREEN("g");
    private final String code;
    Color(String code) { this.code = code; }
    String code() { return code; }
  }
}

record Point(int x, int y) {}

@interface Marker {
  String value() default "";
}
'''
)

PYTHON = _src(
    r'''
"""Module docs mention def not_a_function(): pass and class NotAClass."""
from __future__ import annotations

import os, sys as system
from .models import (
    Candidate,
    Score,
)

MAX_RESULTS = 100
_PATTERN = re.compile(r"\s*def\s+(\w+)")
lower_case_value = 1


@dataclass(frozen=True)
class Weights(Base, metaclass=Meta):
    """Docstring with def fake(): pass."""

    DEFAULT_WEIGHT: float = 0.3

    def __init__(self, table):
        self.table = normalize(table)

    @property
    def total(self) -> float:
        return sum(self.table.values())

    @staticmethod
    async def fetch(
        client,
        key: str = "def not_a_def():",
    ) -> dict[str, list[int]]:
        result = await client.get(key)
        # comment with call(x) and def fake():
        return result

    class Inner:
        def method(self): return helper(1)


def outer(value):
    def inner(item):
        return item * 2

    @register("name", priority=1)
    async def decorated():
        pass

    total = inner(value) + \
        extra(value)
    return total


async def main():
    async with session() as active:
        await active.close()
'''
)
