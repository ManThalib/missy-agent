// Regenerate SDK-verified test vectors for test_raydium_pending.py.
// Usage: node gen_vectors.cjs > ray_vectors.json
// Requires: npm install @raydium-io/raydium-sdk-v2 bn.js
// Deterministic (seeded LCG) so regeneration is reproducible.

const BN = require('bn.js');

// --- SDK math, transcribed verbatim from @raydium-io/raydium-sdk-v2
// src/raydium/clmm/libraries/{position,bigNum,constants}.ts (npm 0.2.73-alpha).
// Inlined because the package's bundled lib/index.js does not load under
// plain require() here; these three functions are pure BN arithmetic.
const Q64 = new BN(1).shln(64);
const Q128 = new BN(1).shln(128);
const wrappingSubU128 = (n0, n1) => n0.add(Q128).sub(n1).mod(Q128);
const mulDivFloor = (a, b, denominator) => a.mul(b).div(denominator);

class PositionUtils {
  static getfeeGrowthInside(poolState, tickLowerState, tickUpperState) {
    let feeGrowthBelowX64A = new BN(0);
    let feeGrowthBelowX64B = new BN(0);
    if (poolState.tickCurrent >= tickLowerState.tick) {
      feeGrowthBelowX64A = tickLowerState.feeGrowthOutsideX64A;
      feeGrowthBelowX64B = tickLowerState.feeGrowthOutsideX64B;
    } else {
      feeGrowthBelowX64A = wrappingSubU128(poolState.feeGrowthGlobalX64A, tickLowerState.feeGrowthOutsideX64A);
      feeGrowthBelowX64B = wrappingSubU128(poolState.feeGrowthGlobalX64B, tickLowerState.feeGrowthOutsideX64B);
    }
    let feeGrowthAboveX64A = new BN(0);
    let feeGrowthAboveX64B = new BN(0);
    if (poolState.tickCurrent < tickUpperState.tick) {
      feeGrowthAboveX64A = tickUpperState.feeGrowthOutsideX64A;
      feeGrowthAboveX64B = tickUpperState.feeGrowthOutsideX64B;
    } else {
      feeGrowthAboveX64A = wrappingSubU128(poolState.feeGrowthGlobalX64A, tickUpperState.feeGrowthOutsideX64A);
      feeGrowthAboveX64B = wrappingSubU128(poolState.feeGrowthGlobalX64B, tickUpperState.feeGrowthOutsideX64B);
    }
    const feeGrowthInsideX64A = wrappingSubU128(
      wrappingSubU128(poolState.feeGrowthGlobalX64A, feeGrowthBelowX64A),
      feeGrowthAboveX64A);
    const feeGrowthInsideBX64 = wrappingSubU128(
      wrappingSubU128(poolState.feeGrowthGlobalX64B, feeGrowthBelowX64B),
      feeGrowthAboveX64B);
    return { feeGrowthInsideX64A, feeGrowthInsideBX64 };
  }

  static GetPositionFees(ammPool, positionState, tickLowerState, tickUpperState) {
    const { feeGrowthInsideX64A, feeGrowthInsideBX64 } =
      this.getfeeGrowthInside(ammPool, tickLowerState, tickUpperState);
    const feeGrowthdeltaA = mulDivFloor(
      wrappingSubU128(feeGrowthInsideX64A, positionState.feeGrowthInsideLastX64A),
      positionState.liquidity, Q64);
    const tokenFeeAmountA = positionState.tokenFeesOwedA.add(feeGrowthdeltaA);
    const feeGrowthdelta1 = mulDivFloor(
      wrappingSubU128(feeGrowthInsideBX64, positionState.feeGrowthInsideLastX64B),
      positionState.liquidity, Q64);
    const tokenFeeAmountB = positionState.tokenFeesOwedB.add(feeGrowthdelta1);
    return { tokenFeeAmountA, tokenFeeAmountB };
  }

  static getRewardGrowthInside(tickCurrentIndex, tickLowerState, tickUpperState, rewardInfos) {
    const rewardGrowthsInside = [];
    for (let i = 0; i < rewardInfos.length; i++) {
      let rewardGrowthsBelow = new BN(0);
      if (tickLowerState.liquidityGross.eqn(0)) {
        rewardGrowthsBelow = rewardInfos[i].growthGlobalX64;
      } else if (tickCurrentIndex < tickLowerState.tick) {
        rewardGrowthsBelow = wrappingSubU128(rewardInfos[i].growthGlobalX64, tickLowerState.rewardGrowthsOutsideX64[i]);
      } else {
        rewardGrowthsBelow = tickLowerState.rewardGrowthsOutsideX64[i];
      }
      let rewardGrowthsAbove = new BN(0);
      if (tickUpperState.liquidityGross.eqn(0)) {
        //
      } else if (tickCurrentIndex < tickUpperState.tick) {
        rewardGrowthsAbove = tickUpperState.rewardGrowthsOutsideX64[i];
      } else {
        rewardGrowthsAbove = wrappingSubU128(rewardInfos[i].growthGlobalX64, tickUpperState.rewardGrowthsOutsideX64[i]);
      }
      rewardGrowthsInside.push(wrappingSubU128(
        wrappingSubU128(rewardInfos[i].growthGlobalX64, rewardGrowthsBelow),
        rewardGrowthsAbove));
    }
    return rewardGrowthsInside;
  }

  static GetPositionRewards(ammPool, positionState, tickLowerState, tickUpperState) {
    const rewards = [];
    const rewardGrowthsInside = this.getRewardGrowthInside(
      ammPool.tickCurrent, tickLowerState, tickUpperState, ammPool.rewardInfos);
    for (let i = 0; i < rewardGrowthsInside.length; i++) {
      const rewardGrowthInside = rewardGrowthsInside[i];
      const currRewardInfo = positionState.rewardInfos[i];
      const rewardGrowthDelta = wrappingSubU128(rewardGrowthInside, currRewardInfo.growthInsideLastX64);
      const amountOwedDelta = mulDivFloor(rewardGrowthDelta, positionState.liquidity, Q64);
      const rewardAmountOwed = currRewardInfo.rewardAmountOwed.add(amountOwedDelta);
      rewards.push(rewardAmountOwed);
    }
    return rewards;
  }
}

// Seeded PRNG for reproducible u128-ish values.
let seed = 0x5eed1234n;
function rnd(bits) {
  let v = 0n;
  for (let i = 0; i < bits; i += 32) {
    seed = (seed * 6364136223846793005n + 1442695040888963407n) & 0xffffffffffffffffn;
    v = (v << 32n) | (seed >> 16n);
  }
  v &= (1n << BigInt(bits)) - 1n;
  if (v === 0n) v = 1n;
  return v;
}
const bn = (bits) => new BN(rnd(bits).toString());
const u128 = () => bn(100);
const u64ish = () => bn(60);

const TICK_LOWER = -200;
const TICK_UPPER = 0;

function mkTick(tick) {
  return {
    tick,
    liquidityGross: new BN(0), // uninitialized semantics, as original generator
    feeGrowthOutsideX64A: u128(),
    feeGrowthOutsideX64B: u128(),
    rewardGrowthsOutsideX64: [u128(), u128(), u128()],
  };
}

function genCase(name, tickCurrent) {
  const pool = {
    tickCurrent,
    feeGrowthGlobalX64A: u128(),
    feeGrowthGlobalX64B: u128(),
    rewardInfos: [
      { growthGlobalX64: u128() },
      { growthGlobalX64: u128() },
      { growthGlobalX64: u128() },
    ],
  };
  const lower = mkTick(TICK_LOWER);
  const upper = mkTick(TICK_UPPER);
  const position = {
    liquidity: u64ish(),
    feeGrowthInsideLastX64A: u128(),
    feeGrowthInsideLastX64B: u128(),
    tokenFeesOwedA: bn(40),
    tokenFeesOwedB: bn(40),
    rewardInfos: [
      { growthInsideLastX64: u128(), rewardAmountOwed: bn(40) },
      { growthInsideLastX64: u128(), rewardAmountOwed: bn(40) },
      { growthInsideLastX64: u128(), rewardAmountOwed: bn(40) },
    ],
  };

  const fees = PositionUtils.GetPositionFees(pool, position, lower, upper);
  const rewards = PositionUtils.GetPositionRewards(pool, position, lower, upper);

  return {
    name,
    tick_current: tickCurrent,
    global_a: pool.feeGrowthGlobalX64A.toString(),
    global_b: pool.feeGrowthGlobalX64B.toString(),
    reward_globals: pool.rewardInfos.map((r) => r.growthGlobalX64.toString()),
    lower_out_a: lower.feeGrowthOutsideX64A.toString(),
    lower_out_b: lower.feeGrowthOutsideX64B.toString(),
    lower_out_r: lower.rewardGrowthsOutsideX64.map((v) => v.toString()),
    upper_out_a: upper.feeGrowthOutsideX64A.toString(),
    upper_out_b: upper.feeGrowthOutsideX64B.toString(),
    upper_out_r: upper.rewardGrowthsOutsideX64.map((v) => v.toString()),
    liquidity: position.liquidity.toString(),
    last_a: position.feeGrowthInsideLastX64A.toString(),
    last_b: position.feeGrowthInsideLastX64B.toString(),
    owed_a: position.tokenFeesOwedA.toString(),
    owed_b: position.tokenFeesOwedB.toString(),
    reward_owed: position.rewardInfos.map((r) => r.rewardAmountOwed.toString()),
    reward_last: position.rewardInfos.map((r) => r.growthInsideLastX64.toString()),
    want_fees: [fees.tokenFeeAmountA.toString(), fees.tokenFeeAmountB.toString()],
    want_rewards: rewards.map((r) => r.toString()),
    generated: 'raydium-sdk-v2 PositionUtils, gen_vectors.cjs (deterministic seed)',
  };
}

const cases = [
  genCase('in_range', -100),
  genCase('below_range', -300),
  genCase('above_range', 100),
];
console.log(JSON.stringify(cases, null, 1));
